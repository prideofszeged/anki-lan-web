#!/usr/bin/env node
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname } from "node:path";

const defaultInput = existsSync("packages/contracts/openapi-v1.json")
  ? "packages/contracts/openapi-v1.json"
  : (existsSync("openapi-v1.snapshot.json") ? "openapi-v1.snapshot.json" : "packages/contracts/openapi-v1.json");

const inputPath = process.argv[2] || defaultInput;
const outputPath = process.argv[3] || "packages/contracts/ts/api.ts";

if (!existsSync(inputPath)) {
  console.error(`Input file not found: ${inputPath}`);
  process.exit(1);
}

const raw = readFileSync(inputPath, "utf-8");
const spec = JSON.parse(raw);

function resolveType(propSchema) {
  if (!propSchema) return "unknown";
  if (propSchema.$ref) {
    return propSchema.$ref.split("/").pop();
  }
  if (propSchema.anyOf) {
    return propSchema.anyOf.map(resolveType).join(" | ");
  }
  if (propSchema.oneOf) {
    return propSchema.oneOf.map(resolveType).join(" | ");
  }
  if (propSchema.type === "string") {
    return "string";
  }
  if (propSchema.type === "integer" || propSchema.type === "number") {
    return "number";
  }
  if (propSchema.type === "boolean") {
    return "boolean";
  }
  if (propSchema.type === "array") {
    const itemType = resolveType(propSchema.items);
    if (itemType.includes("|")) {
      return `(${itemType})[]`;
    }
    return `${itemType}[]`;
  }
  if (propSchema.type === "object") {
    if (propSchema.properties) {
      const entries = Object.entries(propSchema.properties).map(([k, v]) => {
        const req = (propSchema.required || []).includes(k);
        return `${k}${req ? "" : "?"}: ${resolveType(v)};`;
      });
      return `{ ${entries.join(" ")} }`;
    }
    return "Record<string, unknown>";
  }
  return "unknown";
}

function toCamelCase(str) {
  return str.replace(/_([a-z0-9])/g, (_, g) => g.toUpperCase());
}

function deriveCleanName(opId) {
  // e.g. login_api_v1_auth_login_post -> login
  // list_decks_api_v1_decks_get -> listDecks
  // get_deck_api_v1_decks__deck_id__get -> getDeck
  if (opId.includes("_api_v1_")) {
    const prefix = opId.split("_api_v1_")[0];
    return toCamelCase(prefix);
  }
  return toCamelCase(opId);
}

// 1. Generate Component Schemas
const schemas = spec.components?.schemas || {};
const schemaNames = Object.keys(schemas).sort();

const interfaceBlocks = schemaNames.map((name) => {
  const s = schemas[name];
  const required = new Set(s.required || []);
  const properties = s.properties || {};
  const propNames = Object.keys(properties).sort();

  if (propNames.length === 0) {
    return `export interface ${name} {}`;
  }

  const lines = propNames.map((propName) => {
    const propSchema = properties[propName];
    const isReq = required.has(propName);
    const tsType = resolveType(propSchema);
    return `  ${propName}${isReq ? "" : "?"}: ${tsType};`;
  });

  return `export interface ${name} {\n${lines.join("\n")}\n}`;
});

// 2. Parse Operations & Paths Map
const paths = spec.paths || {};
const pathEntries = Object.keys(paths).sort();
const operations = [];

const pathsTypeBlocks = pathEntries.map((pathStr) => {
  const methods = paths[pathStr];
  const methodNames = Object.keys(methods).sort();

  const methodBlocks = methodNames.map((m) => {
    const op = methods[m];
    const opId = op.operationId || `${m}_${pathStr.replace(/[^a-zA-Z0-9]/g, "_")}`;
    const cleanName = deriveCleanName(opId);

    // parameters
    const params = op.parameters || [];
    const pathParams = params.filter((p) => p.in === "path");
    const queryParams = params.filter((p) => p.in === "query");

    // request body
    const reqBodySchema = op.requestBody?.content?.["application/json"]?.schema;
    const reqBodyType = reqBodySchema ? resolveType(reqBodySchema) : undefined;
    const reqBodyRequired = !!op.requestBody?.required;

    // response
    const res200 = op.responses?.["200"]?.content?.["application/json"]?.schema;
    const resType = res200 ? resolveType(res200) : "void";

    operations.push({
      opId,
      cleanName,
      path: pathStr,
      method: m.toUpperCase(),
      pathParams,
      queryParams,
      reqBodyType,
      reqBodyRequired,
      resType,
    });

    let paramsType = "never";
    if (pathParams.length > 0 || queryParams.length > 0) {
      const parts = [];
      if (pathParams.length > 0) {
        const pLines = pathParams.map(
          (p) => `${p.name}${p.required ? "" : "?"}: ${resolveType(p.schema)};`
        );
        parts.push(`path: { ${pLines.join(" ")} }`);
      }
      if (queryParams.length > 0) {
        const qLines = queryParams.map(
          (p) => `${p.name}${p.required ? "" : "?"}: ${resolveType(p.schema)};`
        );
        parts.push(`query: { ${qLines.join(" ")} }`);
      }
      paramsType = `{ ${parts.join("; ")} }`;
    }

    return `    ${m.toLowerCase()}: {\n      parameters: ${paramsType};\n      requestBody: ${
      reqBodyType || "never"
    };\n      response: ${resType};\n    };`;
  });

  return `  "${pathStr}": {\n${methodBlocks.join("\n")}\n  };`;
});

// 3. Generate ApiClient Interface & Client Methods
const clientMethodSignatures = [];
const clientMethodImplementations = [];

for (const op of operations) {
  const args = [];
  for (const p of op.pathParams) {
    args.push(`${p.name}: ${resolveType(p.schema)}`);
  }
  if (op.reqBodyType) {
    args.push(`body${op.reqBodyRequired ? "" : "?"}: ${op.reqBodyType}`);
  }

  const sig = `(${args.join(", ")}): Promise<${op.resType}>`;
  clientMethodSignatures.push(`  ${op.cleanName}${sig};`);
  if (op.opId !== op.cleanName) {
    clientMethodSignatures.push(`  ${op.opId}${sig};`);
  }

  // Implementation
  let pathExpr = `"${op.path}"`;
  if (op.pathParams.length > 0) {
    let pStr = op.path;
    for (const p of op.pathParams) {
      pStr = pStr.replace(`{${p.name}}`, `\${encodeURIComponent(String(${p.name}))}`);
    }
    pathExpr = `\`${pStr}\``;
  }

  const reqArgs = [pathExpr, `"${op.method}"`];
  if (op.reqBodyType) {
    reqArgs.push("body");
  }

  const impl = `(${args.join(", ")}) => request<${op.resType}>(${reqArgs.join(", ")})`;
  clientMethodImplementations.push(`    ${op.cleanName}: ${impl},`);
  if (op.opId !== op.cleanName) {
    clientMethodImplementations.push(`    ${op.opId}: ${impl},`);
  }
}

const fileContent = `/**
 * Generated TypeScript client from OpenAPI specification.
 * DO NOT EDIT MANUALLY. Generated by tools/gen_api_client.mjs
 */

${interfaceBlocks.join("\n\n")}

export interface Paths {
${pathsTypeBlocks.join("\n")}
}

export class ApiError extends Error {
  public status: number;
  public detail: unknown;

  constructor(status: number, detail: unknown, message?: string) {
    const msg =
      message ||
      (typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "detail" in detail
        ? typeof (detail as any).detail === "string"
          ? (detail as any).detail
          : JSON.stringify((detail as any).detail)
        : \`HTTP \${status}\`);
    super(msg);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiClient {
${clientMethodSignatures.join("\n")}
}

export function createClient(baseUrl: string = "", fetchImpl?: typeof fetch): ApiClient {
  const base = baseUrl.replace(/\\/+$/, "");
  const _fetch =
    fetchImpl ||
    (typeof fetch !== "undefined"
      ? fetch.bind(globalThis)
      : (globalThis as any).fetch);

  async function request<T>(
    path: string,
    method: string,
    body?: unknown
  ): Promise<T> {
    const url = \`\${base}\${path}\`;
    const headers: Record<string, string> = {};
    const init: RequestInit = {
      method,
      credentials: "same-origin",
      headers,
    };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    const response = await _fetch(url, init);

    if (!response.ok) {
      let detail: unknown;
      try {
        detail = await response.json();
      } catch {
        try {
          detail = await response.text();
        } catch {
          detail = null;
        }
      }
      throw new ApiError(response.status, detail);
    }

    if (response.status === 204) {
      return undefined as T;
    }

    const text = await response.text();
    if (!text) {
      return undefined as T;
    }

    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }

  const client: ApiClient = {
${clientMethodImplementations.join("\n")}
  };

  return client;
}
`;

mkdirSync(dirname(outputPath), { recursive: true });
writeFileSync(outputPath, fileContent, "utf-8");
console.log(`Generated ${outputPath} from ${inputPath}`);
