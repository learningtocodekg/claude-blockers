"""The sidebar's ordering, checked by running the real static/app.js.

    uv run --with quickjs python tests/test_sidebar_order.py

app.js is browser code, so this stands a minimal DOM in front of it and reads
back the elements it appends. That is worth the stubs: the bug this guards
against was invisible in the source and obvious the moment you saw the rendered
list, because it took a correct sort and a correct grouping and put them in the
wrong order relative to each other.

WHAT WENT WRONG. The server returns one order for every caller -- open first,
then by urgency, then newest -- which is right for the CLI and for board_status.
The sidebar's "Most recent" view drew a day heading whenever the date changed
from the previous row, on the assumption that rows arrived newest-first. They do
not: urgency cuts them into three separately-descending runs. A high-urgency card
from two days ago therefore came first, under its own "Yesterday" heading, then
today's normal cards under "Today", then the rest of yesterday's under a SECOND
"Yesterday" heading -- and a low-urgency card sank below normal ones raised
before it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import quickjs
except ImportError:
    print("SKIP: this test needs a JS engine -- pip install quickjs")
    sys.exit(0)

APP = (Path(__file__).resolve().parent.parent
       / "claude_blockers" / "static" / "app.js").read_text(encoding="utf-8")

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not condition else ""))


# --------------------------------------------------------------------------- #
# Enough of a browser for app.js to load and for renderList to build into.
# --------------------------------------------------------------------------- #

STUBS = r"""
function El() {
  this.children = []; this.className = ''; this.textContent = ''; this._html = '';
  this.style = { setProperty: function(){}, removeProperty: function(){} };
  this.classList = { toggle: function(){}, add: function(){}, remove: function(){},
                     contains: function(){ return false; } };
  this.dataset = {};
}
El.prototype.addEventListener = function(){};
El.prototype.setAttribute = function(){};
El.prototype.appendChild = function(c){ this.children.push(c); return c; };
El.prototype.querySelectorAll = function(){ return []; };
El.prototype.querySelector = function(){ return null; };
El.prototype.closest = function(){ return null; };
El.prototype.focus = function(){};
El.prototype.remove = function(){};
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function(){ return this._html; },
  set: function(v){ this._html = v; this.children = []; }
});

var _els = {};
var document = {
  title: '',
  getElementById: function(id){ if (!_els[id]) _els[id] = new El(); return _els[id]; },
  createElement: function(){ return new El(); },
  addEventListener: function(){}, removeEventListener: function(){},
  querySelector: function(){ return null; }, querySelectorAll: function(){ return []; }
};
var window = { focus: function(){} };
var location = { search: '', hash: '', pathname: '/' };
var history = { replaceState: function(){} };
var navigator = { clipboard: { writeText: function(){ return Promise.resolve(); } } };
function fetch(){ return Promise.reject(new Error('offline in test')); }
function setInterval(){ return 0; }
function setTimeout(){ return 0; }
function URLSearchParams(){ this.get = function(){ return null; };
                            this.set = function(){};
                            this.toString = function(){ return ''; }; }
"""

# Walks what renderList appended and reports it as a flat list: 'day <label>',
# 'group <name>', or 'row <id> <high|->'.
DRIVER = r"""
function renderSidebar(payloadJson, view) {
  var data = JSON.parse(payloadJson);
  indexProjects(data.projects);
  state.last = data;
  state.view = view;
  state.selectedId = null;
  renderList(data);

  var out = [];
  var walk = function (el) {
    for (var i = 0; i < el.children.length; i++) {
      var c = el.children[i], cls = String(c.className);
      if (cls === 'day-divider') { out.push('day ' + c.textContent); continue; }
      if (cls.indexOf('grp-head') === 0) {
        var g = /class="grp-name">([^<]*)</.exec(c.innerHTML);
        out.push('group ' + (g ? g[1] : '?'));
        continue;
      }
      if (cls.indexOf('row') === 0) {
        var m = /#(\d+)/.exec(c.innerHTML);
        out.push('row ' + (m ? m[1] : '?') +
                 (c.innerHTML.indexOf('rhot') !== -1 ? ' high' : ' -'));
        continue;
      }
      walk(c);
    }
  };
  walk(document.getElementById('side-list'));
  return out.join('\n');
}
"""


# QuickJS ships a minimal Intl, so an older day prints as 08/09/2026 here where a
# browser gives "August 9". Nothing is asserted about the wording -- only that
# each label appears once and that the rows under it are in order.
def render(payload: dict, view: str) -> list[str]:
    ctx = quickjs.Context()
    ctx.eval(STUBS)
    ctx.eval(APP)
    ctx.eval(DRIVER)
    text = ctx.get("renderSidebar")(json.dumps(payload), view)
    return [line for line in text.split("\n") if line]


# --------------------------------------------------------------------------- #
# A board shaped like the one that exposed the bug. Timestamps are relative to
# now so the Today/Yesterday branches stay live and the test does not expire.
# --------------------------------------------------------------------------- #

def iso(hours_ago: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


DAY = 24.0
ROWS = [
    # id,  urgency,  raised,          project
    (64,  "high",   iso(2 * DAY + 1), "acme-audio"),   # oldest, but urgent
    (123, "normal", iso(0.2),         "acme-site"),       # newest
    (119, "normal", iso(0.3),         "acme-audio"),
    (89,  "normal", iso(11),          "acme-audio"),
    (80,  "normal", iso(12),          "acme-docs"),
    (74,  "normal", iso(2 * DAY + 2), "acme-audio"),
    (65,  "normal", iso(2 * DAY + 3), "acme-audio"),
    (70,  "low",    iso(2 * DAY + 1.5), "acme-audio"),  # newer than #74 and #65
]
BY_ID = {r[0]: r for r in ROWS}

PAYLOAD = {
    # Handed over in the server's order: urgency outranks the clock.
    "blockers": [
        {"id": i, "urgency": u, "created_at": c, "project": p, "kind": "blocker",
         "status": "open", "seen": 1, "branch": None, "title": f"blocker {i}"}
        for i, u, c, p in sorted(
            ROWS, key=lambda r: ({"high": 0, "normal": 1, "low": 2}[r[1]],
                                 [-ord(ch) for ch in r[2]]))
    ],
    "projects": [{"project": n, "open_count": 1}
                 for n in ("acme-audio", "acme-site", "acme-docs")],
    "stats": {"open": len(ROWS), "total": len(ROWS), "resolved": 0, "dismissed": 0},
    "sessions": {"live": 0},
    "version": "test",
}

# --------------------------------------------------------------------------- #

print("\nMost recent")
recent = render(PAYLOAD, "recent")
for line in recent:
    print(f"    {line}")

days = [line for line in recent if line.startswith("day ")]
check("every day heading appears once", len(days) == len(set(days)),
      f"got {days}")
check("more than one day is on the board", len(days) > 1, f"got {days}")

order = [int(line.split()[1]) for line in recent if line.startswith("row ")]
stamps = [BY_ID[i][2] for i in order]
check("rows run newest to oldest", stamps == sorted(stamps, reverse=True),
      f"got {order}")
check("every blocker is drawn exactly once", sorted(order) == sorted(BY_ID),
      f"got {order}")
check("the low-urgency card sits by its date, not last",
      order.index(70) < order.index(74), f"got {order}")
check("the urgent card is marked, now that it is not pinned to the top",
      "row 64 high" in recent, f"got {[l for l in recent if '64' in l]}")

print("\nBy project")
grouped = render(PAYLOAD, "project")
for line in grouped:
    print(f"    {line}")

groups: dict[str, list[int]] = {}
current = None
for line in grouped:
    if line.startswith("group "):
        current = line.split(" ", 1)[1]
        groups[current] = []
    elif line.startswith("row "):
        groups[current].append(int(line.split()[1]))

check("groups run largest first, then by name",
      list(groups) == ["acme-audio", "acme-docs", "acme-site"], f"got {list(groups)}")

gorder = [i for ids in groups.values() for i in ids]
check("every blocker is drawn exactly once when grouped",
      sorted(gorder) == sorted(BY_ID), f"got {groups}")
check("rows land in their own project's group",
      all(BY_ID[i][3] == name for name, ids in groups.items() for i in ids),
      f"got {groups}")
# Inside a group the board's own priority order is what you want: this is the
# view you open to see what a project needs, not when it asked.
check("urgency leads inside a group", groups["acme-audio"][0] == 64,
      f"got {groups['acme-audio']}")
check("low urgency trails inside a group", groups["acme-audio"][-1] == 70,
      f"got {groups['acme-audio']}")

# --------------------------------------------------------------------------- #
print(f"\n{'=' * 58}")
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"    FAILED: {name}")
print(f"{'=' * 58}\n")
sys.exit(1 if FAIL else 0)
