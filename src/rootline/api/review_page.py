from html import escape

from rootline.review import Draft

STYLE = """
:root { --bg: #fff; --fg: #1f2328; --muted: #59636e; --line: #d1d9e0; --open: #b42318; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #3d444d; --open: #f97066; }
}
body { background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif;
  max-width: 960px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; } .muted { color: var(--muted); }
.draft { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; margin: 16px 0; }
.draft.open { border-color: var(--open); }
pre { white-space: pre-wrap; max-height: 360px; overflow: auto; border-top: 1px solid var(--line);
  padding-top: 8px; font-size: 13px; }
form { display: inline; } button { margin-right: 8px; padding: 6px 12px; cursor: pointer; }
"""


def review_page(drafts: list[Draft], others: dict[str, int]) -> str:
    waiting = [d for d in drafts if d.status == "open"]
    cards = (
        "\n".join(card(draft) for draft in drafts) or "<p class='muted'>No drafts to review.</p>"
    )
    other = ", ".join(f"{escape(reason)} {count}" for reason, count in others.items())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rootline review</title><style>{STYLE}</style></head>
<body>
<h1>Review</h1>
<p class="muted">Drafts are written from incident chats and are never searchable until a person
approves them here. {len(waiting)} waiting.{f" Other open items: {other}." if other else ""}</p>
{cards}
</body></html>"""


def card(draft: Draft) -> str:
    actions = (
        f"""<form method="post" action="/review/{draft.id}/approve">
<button>Approve and index</button></form>
<form method="post" action="/review/{draft.id}/reject"><button>Reject</button></form>"""
        if draft.status == "open"
        else f"<span class='muted'>{escape(draft.status)}</span>"
    )
    return f"""<div class="draft {escape(draft.status)}">
<p><strong>{escape(draft.doc_id)}</strong> [{escape(draft.doc_type.upper())}]
{escape(draft.title)} <span class="muted">drafted {draft.created_at:%Y-%m-%d %H:%M}</span></p>
{actions}
<pre>{escape(draft.markdown)}</pre>
</div>"""
