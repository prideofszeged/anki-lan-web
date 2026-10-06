from __future__ import annotations
import html
import json
from ankiweb.i18n import tr
from ankiweb.screens.editor import (
    _munge,
    editor_links_js,
    editor_state_payload,
    paste_handler_js,
)
from ankiweb.ankiconnect.actions._helpers import check_addable
from ankiweb.collection_service import op_changes_to_flags

_STYLE = (
    "<style>"
    "body[data-context=\"add\"]{padding-top:42px!important;padding-bottom:max(32px,env(safe-area-inset-bottom,0px));"
    "background:var(--surface);color:var(--text);min-height:100dvh}"
    "body[data-context=\"add\"] #add-chrome{position:relative;top:0;left:auto;right:auto;height:auto;"
    "z-index:1100;padding:0;background:color-mix(in srgb,var(--surface) 94%,transparent);"
    "border-bottom:1px solid var(--border);backdrop-filter:blur(12px)}"
    "#add-chrome .add-chrome-inner{width:min(100% - 32px,960px);margin:0 auto;padding:14px 0;"
    "display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px 20px;align-items:end}"
    ".add-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;grid-column:1/-1}"
    ".add-heading h1{font-size:1.25rem;line-height:1.2;margin:0}.add-heading p{color:var(--muted);"
    "font-size:.85rem;margin:4px 0 0}.add-selectors{display:grid;grid-template-columns:minmax(0,1fr) "
    "minmax(0,1fr);gap:12px}.add-control{display:flex;flex-direction:column;gap:6px;min-width:0;"
    "color:var(--muted);font-size:.8rem;font-weight:650}.add-control select{width:100%;min-width:0;"
    "min-height:44px;padding:8px 36px 8px 11px;border:1px solid var(--border);border-radius:10px;"
    "background:var(--surface);color:var(--text)}.add-actions{display:flex;align-items:center;gap:10px}"
    "#add-btn{min-height:44px;background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}"
    "#add-close{min-height:44px;display:inline-flex;align-items:center;padding:8px 12px;border-radius:10px;"
    "color:var(--text);text-decoration:none;border:1px solid var(--border);background:var(--surface-soft)}"
    "#add-toast{grid-column:1/-1;min-height:20px;color:#15803d;font-weight:650;font-size:.86rem}"
    "body[data-context=\"add\"] .note-editor{width:min(100% - 32px,960px)!important;max-width:960px!important;"
    "display:block!important;height:auto!important;min-height:0!important;margin:16px auto 96px!important;overflow:visible!important;"
    "background:var(--surface)!important;color:var(--text)!important}"
    "body[data-context=\"add\"] .editor-toolbar{width:100%!important;max-width:100%!important;"
    "min-height:48px;overflow-x:auto!important;overflow-y:hidden!important;overscroll-behavior-inline:contain;"
    "scrollbar-width:thin;background:var(--surface)!important;border-color:var(--border)!important}"
    "body[data-context=\"add\"] .editor-toolbar .button-toolbar{display:flex!important;flex-wrap:nowrap!important;"
    "width:max-content!important;min-width:max-content!important;max-width:none!important}"
    "body[data-context=\"add\"] .editor-toolbar button{min-width:44px!important;min-height:44px!important;"
    "color:var(--text)!important;border-color:var(--border)!important;background:var(--surface-soft)!important}"
    "body[data-context=\"add\"] .editor-toolbar button:disabled{color:var(--muted)!important;opacity:.72}"
    "body[data-context=\"add\"] .fields{height:auto!important;overflow:visible!important;flex-grow:0!important;"
    "padding:0!important;margin:12px 0!important;gap:12px!important}"
    "body[data-context=\"add\"] .scroll-area-relative{height:auto!important;min-height:0!important;"
    "flex-grow:0!important;position:relative!important}body[data-context=\"add\"] .scroll-area{"
    "position:static!important;height:auto!important;overflow:visible!important}"
    "body[data-context=\"add\"] .field-container{width:100%!important;max-width:100%!important;"
    "background:var(--surface-soft)!important;color:var(--text)!important;border:1px solid var(--border)!important;"
    "border-radius:12px!important;overflow:hidden!important}"
    "body[data-context=\"add\"] .editor-field{min-height:112px!important;background:var(--surface)!important;"
    "color:var(--text)!important;border-color:var(--border)!important;box-shadow:none!important}"
    "body[data-context=\"add\"] .rich-text-editable{display:block;min-height:108px!important;"
    "background:var(--surface)!important;color:var(--text)!important}"
    "body[data-context=\"add\"] :is(button,a,select):focus-visible{outline:3px solid "
    "color-mix(in srgb,var(--accent),transparent 45%)!important;outline-offset:2px!important}"
    "@media(max-width:639px){body[data-context=\"add\"]{padding-top:0!important;padding-bottom:"
    "calc(124px + env(safe-area-inset-bottom,0px))!important}body[data-context=\"add\"] #add-chrome{"
    "position:sticky!important;top:0!important;padding:0!important;min-height:0!important;backdrop-filter:none}"
    "#add-chrome .add-chrome-inner{width:100%;padding:12px max(14px,env(safe-area-inset-right,0px)) "
    "12px max(14px,env(safe-area-inset-left,0px));display:block}"
    ".add-heading{margin-bottom:12px}.add-heading p{display:none}.add-selectors{grid-template-columns:1fr;gap:10px}"
    ".add-control{display:flex!important;align-items:stretch!important;gap:5px!important;font-size:.78rem!important}"
    "#add-chrome .add-control select{width:100%!important;max-width:none!important;min-height:44px!important}"
    ".add-actions{display:block}.add-actions #add-close{position:absolute;top:9px;right:max(14px,"
    "env(safe-area-inset-right,0px));min-height:44px!important}.add-actions #add-btn{position:fixed!important;"
    "bottom:calc(56px + env(safe-area-inset-bottom,0px) + 8px)!important;left:max(16px,"
    "env(safe-area-inset-left,0px))!important;right:max(16px,env(safe-area-inset-right,0px))!important;"
    "width:auto!important;min-height:48px!important;z-index:2100!important}#add-toast{margin-top:8px}"
    "body[data-context=\"add\"] .note-editor{width:100%!important;max-width:100%!important;margin:0 auto 32px!important;"
    "padding:0 8px!important}body[data-context=\"add\"] .editor-toolbar{margin-inline:-8px!important;"
    "width:calc(100% + 16px)!important;max-width:calc(100% + 16px)!important;padding-inline:8px!important}"
    "body[data-context=\"add\"] .editor-field{min-height:96px!important}body[data-context=\"add\"] "
    ".rich-text-editable{min-height:92px!important}}"
    "</style>"
)


def _empty_load(col, ntid: int) -> dict:
    model = col.models.get(ntid)
    flds = model["flds"]
    return editor_state_payload(col, model, [[f["name"], ""] for f in flds], [], 0)


def load_data_for_spec(col, note_spec) -> dict | None:
    """Build the `ankiwebLoadNote` payload for an AnkiConnect note spec
    (modelName/fields/tags) — used by guiAddCards/guiAddNoteSetData to live-prefill
    the open Add dialog. Returns None if the model is unknown (case-insensitive fields)."""
    spec = note_spec or {}
    model = col.models.by_name(spec.get("modelName", "")) if spec.get("modelName") else None
    if model is None:
        return None
    d = _empty_load(col, model["id"])
    by_lower = {f["name"].lower(): i for i, f in enumerate(model["flds"])}
    for key, val in (spec.get("fields") or {}).items():
        i = by_lower.get(str(key).lower())
        if i is not None:
            d["fields"][i][1] = val
    d["tags"] = list(spec.get("tags") or [])
    return d


def add_page_body(deck_opts: str, nt_opts: str) -> str:
    return (
        _STYLE +
        "<header id='add-chrome' aria-labelledby='add-page-title'><div class='add-chrome-inner'>"
        "<div class='add-heading'><div><h1 id='add-page-title'>" + tr.actions_add_note() +
        "</h1><p>Create a card in the selected deck and note type.</p></div></div>"
        "<div class='add-selectors'>"
        f"<label class='add-control' for='add-deck'><span>{tr.decks_deck()}</span><select id='add-deck' "
        "onchange=\"window.pycmd('setdeck:'+this.value)\">" + deck_opts + "</select></label>"
        f"<label class='add-control' for='add-notetype'><span>{tr.notetypes_type()}</span><select id='add-notetype' "
        "onchange=\"window.__ankiwebNotetypeId=this.value;window.pycmd('setnotetype:'+this.value)\">" + nt_opts + "</select></label>"
        "</div><div class='add-actions'>"
        f"<button type='button' id='add-btn' onclick='ankiwebAddNote()'>{tr.actions_add_note()}</button>"
        f"<a id='add-close' href='/deckbrowser'>{tr.actions_close()}</a></div>"
        "<span id='add-toast' role='status' aria-live='polite'></span></div></header>"
        "<script>(function(){"
        "window.setupEditor('add',true);"
        "var _nt=document.getElementById('add-notetype');"
        "if(_nt)window.__ankiwebNotetypeId=_nt.value;"
        "var b=window.__ankiwebBridge;"
        "function readAllFields(){"
        "var cs=Array.prototype.slice.call(document.querySelectorAll('.field-container'));"
        "cs.sort(function(a,b){return Number(a.dataset.index)-Number(b.dataset.index);});"
        "return cs.map(function(fc){var h=fc.querySelector('.rich-text-editable');"
        "if(!h||!h.shadowRoot)return '';"
        "var e=h.shadowRoot.querySelector('[contenteditable]');return e?e.innerHTML:'';});}"
        "window.ankiwebAddNote=function(){window.pycmd('addnote:'+JSON.stringify(readAllFields()));};"
        "b.registerCalls({"
        "ankiwebLoadNote:function(d){require('anki/ui').loaded.then(function(){"
        "var names=d.fields.map(function(f){return f[0];});"
        "var values=d.fields.map(function(f){return f[1];});"
        "window.setNotetypeMeta(d.meta);"
        "window.setFields(names,values);window.setIsImageOcclusion(d.io);window.setFonts(d.fonts);"
        "window.setCollapsed(d.collapsed);window.setClozeFields(d.clozeFields);"
        "window.setPlainTexts(d.plainTexts);window.setDescriptions(d.descriptions);"
        "window.setNoteId(d.noteId);window.setTags(d.tags);"
        "window.setTagsCollapsed(false);window.setMathjaxEnabled(d.mathjax);"
        "window.setShrinkImages(d.shrinkImages);window.setCloseHTMLTags(d.closeHtmlTags);"
        "window.triggerChanges();});},"
        "ankiwebToast:function(m){var t=document.getElementById('add-toast');if(t){"
        "t.textContent=String(m);setTimeout(function(){t.textContent='';},2000);}}"
        "});"
        "require('anki/ui').loaded.then(function(){window.pycmd('addReady');});"
        + paste_handler_js()
        + editor_links_js() +
        "})();</script>"
    )


def render_add_html(col) -> str:
    cur_nt = col.models.current()["id"]
    cur_did = col.decks.get_current_id()
    decks = "".join(
        f"<option value='{d.id}'{' selected' if d.id == cur_did else ''}>{html.escape(d.name)}</option>"
        for d in col.decks.all_names_and_ids())
    nts = "".join(
        f"<option value='{m.id}'{' selected' if m.id == cur_nt else ''}>{html.escape(m.name)}</option>"
        for m in col.models.all_names_and_ids())
    return add_page_body(decks, nts)


def make_add_handler(service, hub):
    state = {"notetype_id": None, "deck_id": None, "tags": []}

    async def handler(arg: str):
        head, _, rest = arg.partition(":")
        if head == "addReady":
            def init(col):
                ntid = col.models.current()["id"]
                did = col.decks.get_current_id()
                return ntid, did, _empty_load(col, ntid)
            ntid, did, data = await service.run(init)
            state.update(notetype_id=ntid, deck_id=did, tags=[])
            await hub.push_call("add", "ankiwebLoadNote", [data])
        elif head == "setnotetype":
            ntid = int(rest)
            state["notetype_id"] = ntid
            state["tags"] = []
            data = await service.run(lambda col: _empty_load(col, ntid))
            await hub.push_call("add", "ankiwebLoadNote", [data])
        elif head == "setdeck":
            state["deck_id"] = int(rest)
        elif head == "saveTags":
            state["tags"] = json.loads(rest)
        elif head == "addnote":
            fields = json.loads(rest)
            ntid, did, tags = state["notetype_id"], state["deck_id"], list(state["tags"])

            def add(col):
                model = col.models.get(ntid)
                note = col.new_note(model)
                for i, h in enumerate(fields):
                    if i < len(note.fields):
                        note.fields[i] = _munge(col, h)
                note.tags = tags
                ok, err = check_addable(col, note, None)
                if not ok:
                    return (None, err), None
                op = col.add_note(note, did)
                return (note.id, None), op
            (nid, err), op = await service.run(add)
            if op is not None:
                flags = op_changes_to_flags(getattr(op, "changes", op))
                if any(flags.values()):
                    await service.emit(flags, "add")
            if err:
                await hub.push_call("add", "ankiwebToast", [err])
            else:
                data = await service.run(lambda col: _empty_load(col, ntid))
                await hub.push_call("add", "ankiwebLoadNote", [data])
                await hub.push_call("add", "ankiwebToast", [tr.adding_added()])
        return None

    return handler
