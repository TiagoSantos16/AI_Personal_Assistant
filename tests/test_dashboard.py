import importlib
from types import SimpleNamespace
from streamlit.testing.v1 import AppTest


def app(monkeypatch, records=(), memory=()):
    dashboard = importlib.import_module("dashboard")
    dashboard.collection.clear()
    calls = []
    def storage(op, payload=None):
        calls.append(op)
        if op == "revision":
            return 1
        if op == 'memory_get':
            return {'revision': 0, 'items': list(memory)}
        if op == 'memory_revision':
            return 0
        if op == 'recall_get':
            return {'revision': 0, 'rules': []}
        if op == "list":
            return {"revision": 1, "records": list(records)}
        if op == "read":
            return {**next(r for r in records if r["name"] == payload["name"]), "content": "# Receita\n\n2 ovos\n<details><summary>SPOILER</summary>Ending</details>"}
        return []
    monkeypatch.setattr(dashboard, "storage", storage)
    monkeypatch.setattr(dashboard, "device_cookie", lambda **kwargs: SimpleNamespace(saved=kwargs["data"]["token"]))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "owner")
    return AppTest.from_string("import dashboard; dashboard.main()", default_timeout=10), calls


def sign_in(at):
    at.run()
    at.text_input[0].set_value("owner")
    at.button[0].click().run()
    return at


def test_login_before_reading(monkeypatch):
    at, calls = app(monkeypatch)
    at.run()
    assert not calls and not at.exception
    at.text_input[0].set_value("wrong")
    at.button[0].click().run()
    assert not calls and at.error
    sign_in(at)
    assert "list" in calls and not at.exception


def test_login_missing_configuration(monkeypatch):
    at, calls = app(monkeypatch)
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    at.run()
    assert at.error and not calls


def test_navigation_costs_and_previous_version(monkeypatch):
    records = [{"name": "previous.md", "title": "Receita", "section": "failed", "status": "superseded",
        "category": "culinary", "saved": 1, "version_id": "v1", "accounting": [], "url": "https://instagram.com/p/abc/"}]
    at, _ = app(monkeypatch, records)
    sign_in(at)
    at.radio[0].set_value("failed").run()
    assert not at.exception
    assert "Previous version" in str(at.dataframe[0].value)
    at.query_params["note"] = "previous.md"
    at.run()
    assert not at.exception
    assert any(e.label == "Cost details" for e in at.expander)
    assert any(e.label == "Generation Info" for e in at.expander)
    assert not at.segmented_control
    labels = [b.label for b in at.button]
    assert "Redo" in labels and "Delete" in labels and "Back to collection" in labels
    assert any("Previous version" in info.value for info in at.info)
    assert at.query_params["note"] == "previous.md"


def test_legacy_metadata_parser():
    from dashboard import parse_metadata
    parsed = parse_metadata("# Old\n- **Source URL:** https://instagram.com/p/abc/\n- **Creator:** José\n**Frames (1):**\n- `old.jpg`\n**Transcript:**\nOlá\n")
    assert parsed["uploader"] == "José" and parsed["frame_images"] == ["old.jpg"]


def test_login_attempt_limit(monkeypatch):
    at, calls = app(monkeypatch)
    at.run()
    for _ in range(5):
        at.text_input[0].set_value("wrong")
        at.button[0].click().run()
    at.run()
    assert at.button[0].disabled and not calls


def test_selection_survives_detail_navigation(monkeypatch):
    records = [{"name": "recipe.md", "title": "Recipe", "section": "notes", "status": "saved",
        "category": "culinary", "saved": 1, "version_id": "v1", "accounting": None}]
    at, _ = app(monkeypatch, records)
    sign_in(at)
    at.session_state["selected_notes"] = {"notes": ["recipe.md"]}
    at.run()
    at.query_params["note"] = "recipe.md"
    at.run()
    next(b for b in at.button if b.label == "Back to collection").click().run()
    assert at.session_state["selected_notes"]["notes"] == ["recipe.md"] and not at.exception


def test_chat_main_page_and_settings(monkeypatch):
    at, _ = app(monkeypatch)
    sign_in(at)
    at.segmented_control[0].set_value("Chatbot").run()
    assert not at.exception and len(at.chat_input) == 1
    assert any(e.label == "Settings" for e in at.expander)
    assert not at.multiselect
    assert not any(w.label == 'Search notes' for w in at.text_input)


def test_table_columns_and_simple_delete(monkeypatch):
    records = [{"name": "recipe.md", "title": "Recipe", "section": "notes", "status": "saved",
        "category": "culinary", "saved": 1, "version_id": "v1", "accounting": None}]
    at, calls = app(monkeypatch, records)
    sign_in(at)
    assert list(at.dataframe[0].value.columns) == ["Title", "Category", "Creator", "Saved"]
    assert not any(b.label == "Delete" for b in at.button)
    at.session_state["selected_notes"] = {"notes": ["recipe.md"]}
    at.run()
    next(b for b in at.button if b.label == "Delete").click().run()
    assert not at.exception and "delete" not in calls
    assert any(b.label == "Cancel" for b in at.button)
    next(b for b in at.button if b.label == "Cancel").click().run()
    assert "delete" not in calls
    next(b for b in at.button if b.label == "Delete").click().run()
    next(b for b in at.button if b.key == "confirm-delete").click().run()
    assert "delete" in calls and not at.exception


def test_detail_delete_and_account_label(monkeypatch):
    records = [{"name": "recipe.md", "title": "Recipe", "section": "notes", "status": "saved",
        "category": "culinary", "saved": 1, "version_id": "v1", "accounting": None,
        "creator": "A very long account name", "creator_url": "https://instagram.com/creator/"}]
    at, calls = app(monkeypatch, records)
    sign_in(at)
    at.query_params["note"] = "recipe.md"
    at.run()
    assert not at.segmented_control and not at.exception
    assert any(link.label == "Account" for link in at.get("link_button"))
    next(b for b in at.button if b.label == "Delete").click().run()
    assert "delete" not in calls
    next(b for b in at.button if b.key == "confirm-delete").click().run()
    assert "delete" in calls and not at.query_params.get("note") and not at.exception


def test_chat_followup_memory_and_source_dropdown(monkeypatch):
    from core import rag
    histories = []
    def answer(question, rows, history=None, personal_memory=None):
        histories.append(list(history or []))
        return {"answer": "## Ingredients\n- 2 eggs [S1]", "sources": ["recipe.md"],
                "source_labels": {"S1": "recipe.md"}, "source_titles": {"recipe.md": "Egg recipe"}}
    monkeypatch.setattr(rag, "answer", answer)
    monkeypatch.setattr(rag, "still_visible", lambda result, records: result)
    at, _ = app(monkeypatch)
    sign_in(at)
    at.segmented_control[0].set_value("Chatbot").run()
    at.chat_input[0].set_value("What is the recipe?").run()
    assert not at.exception and not histories[0]
    assert any(e.label == "Sources" for e in at.expander)
    assert any(link.label == "Egg recipe" for link in at.get("link_button"))
    at.chat_input[0].set_value("How many eggs?").run()
    assert not at.exception and len(histories[1]) == 2
    assert histories[1][0]["content"] == "What is the recipe?"
    assert len(at.session_state["chat_messages"]) == 4


def test_dashboard_answers_receive_personal_memory(monkeypatch):
    from core import rag
    fact = {'id': 'arrival', 'text': 'I watched Arrival.', 'date': 123}
    def answer(question, rows, history=None, personal_memory=None):
        assert personal_memory == [fact]
        return {'answer': 'Personal memory: you watched Arrival [S100000].', 'sources': ['memory:arrival'],
                'source_labels': {'S100000': 'memory:arrival'}}
    monkeypatch.setattr(rag, 'answer', answer)
    at, calls = app(monkeypatch, memory=[fact])
    sign_in(at)
    at.segmented_control[0].set_value('Chatbot').run()
    at.chat_input[0].set_value('Did I watch Arrival?').run()
    assert not at.exception and 'memory_revision' in calls
    assert any('you watched Arrival' in m.value for m in at.markdown)
    assert not any(link.label == 'memory:arrival' for link in at.get('link_button'))


def test_linked_title_preserves_identifier_and_unicode():
    import re
    from urllib.parse import urlsplit, parse_qs
    from dashboard import note_title_link
    name = "Chef's receita & #1.md"
    title = "Pão & ovos? #1 / <script>"
    link = note_title_link(name, title)
    assert parse_qs(urlsplit(link).query) == {"note": [name]}
    assert re.search(r"#title=(.*)", link)[1] == title
    assert link.startswith("?note=")


def test_cost_details_show_reported_unknown_and_compute_usage():
    at = AppTest.from_string('''
import dashboard
dashboard.cost_details({'accounting': [
    {'record_id': 'paid', 'step': 'writer', 'model': 'mock', 'cost': .000041, 'cost_status': 'reported'},
    {'record_id': 'unknown', 'step': 'critic', 'cost': None, 'cost_status': 'unknown'},
    {'record_id': 'cache', 'step': 'cached_evidence', 'cost': 0, 'cost_status': 'reused'}],
    'compute': {'status': 'unknown', 'details': [{'resource': 'cpu', 'seconds': 12, 'estimate': None}]}})
''').run()
    assert not at.exception
    assert any('$0.00004100' in c.value and 'incomplete' in c.value for c in at.caption)
    assert len(at.dataframe[0].value) == 3
    assert at.dataframe[0].value.iloc[0]['cost'] == .000041
    assert at.dataframe[1].value.iloc[0]['seconds'] == 12


def test_missing_costs_remain_explicit_and_do_not_hide_compute():
    at = AppTest.from_string('''
import dashboard
dashboard.cost_details({'accounting': [], 'compute': {'status': 'estimated', 'estimate': .01,
    'details': [{'resource': 'cpu', 'seconds': 10, 'estimate': .01}]}})
''').run()
    assert not at.exception and at.info
    assert 'No AI charges were recorded' in at.info[0].value
    assert 'Cost not recorded' in at.caption[0].value
    assert len(at.dataframe) == 1


def test_selected_media_is_lazy_compact_and_keeps_original_resolution(monkeypatch):
    import dashboard
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new('RGB', (900, 1600), '#376457').save(buffer, 'PNG')
    calls = []
    monkeypatch.setattr(dashboard, 'storage', lambda op, p: calls.append(op) or [buffer.getvalue(), buffer.getvalue()])
    at = AppTest.from_string('''
import dashboard
dashboard.selected_media({'name': 'media.md', 'section': 'notes', 'title': 'Example'})
''').run()
    assert not calls and not at.exception
    at.toggle[0].set_value(True).run()
    assert calls == ['media'] and not at.exception
    images = at.get('image')
    assert len(images) == 2
    # Width is constrained by the parent container, not a lossy 180px image resize.
    assert all(i.proto.imgs[0].alt.startswith('Selected source image') for i in images)
    assert any('expand icon' in c.value for c in at.caption)
    at.run()
    assert len(at.get('image')) == 2


def test_needs_review_notes_are_listed_newest_first_and_back_clears_filters(monkeypatch):
    records = [
        {'name': 'old.md', 'title': 'Old recipe', 'section': 'notes', 'status': 'saved', 'saved': 10,
         'category': 'culinary', 'version_id': 'v1', 'accounting': None},
        {'name': 'new.md', 'title': 'Stock summary', 'section': 'notes', 'status': 'saved', 'saved': 20,
         'category': 'finance', 'version_id': 'v2', 'review_status': 'needs_review', 'accounting': [],
         'coverage': ['Visual text for media 9-12 is incomplete (ProviderChainError).']}]
    at, _ = app(monkeypatch, records)
    sign_in(at)
    assert 'Stock summary' in at.dataframe[0].value.iloc[0]['Title']
    at.selectbox[0].set_value('culinary').run()
    at.radio[0].set_value('failed').run()
    at.query_params['note'] = 'new.md'
    at.run()
    assert not at.exception and any('Saved in Notes' in w.value for w in at.warning)
    assert any(b.label == 'Retry missing media' for b in at.button)
    assert any(e.label == 'Cost details' for e in at.expander)
    next(b for b in at.button if b.label == 'Back to collection').click().run()
    assert not at.exception
    assert at.selectbox[0].value == 'All' and at.radio[0].value == 'notes'
    assert len(at.dataframe[0].value) == 2
    assert 'Stock summary' in at.dataframe[0].value.iloc[0]['Title']


def test_media_loading_failure_does_not_hide_cost_details(monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard, 'storage', lambda *a, **kw: (_ for _ in ()).throw(OSError('unavailable')))
    at = AppTest.from_string('''
import dashboard
note = {'name': 'media.md', 'section': 'notes', 'title': 'Example'}
dashboard.selected_media(note)
dashboard.cost_details(note)
''').run()
    at.toggle[0].set_value(True).run()
    assert not at.exception and at.warning
    assert any(e.label == 'Cost details' for e in at.expander)
    assert any('No AI charges were recorded' in i.value for i in at.info)
