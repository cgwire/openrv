"""
Run the plugin through a fixed scenario and print what it does.

Do not run this script directly. Use `test_behaviour.py`, which gives it a
temporary home directory and compares the output with the expected snapshot.

The script uses the real converter, the real model and the real Qt panel in
offscreen mode. It replaces `rv`, `gazu`, `requests` and `keyring` with stubs.
"""

import copy
import itertools
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import annotation_converter as ac  # noqa: E402

WIDTH, HEIGHT = 1920, 1080
COLOR = [[1.0, 0.2, 0.3, 1.0]]

ERASER_GROUP = {
    "type": "eraser",
    "left": 0,
    "top": 0,
    "width": 100,
    "height": 100,
    "originX": "center",
    "originY": "center",
    "objects": [
        {
            "type": "path",
            "path": "M 10 10 l 5 5 H 40 V 50 q 5 5 10 0 c 1 2 3 4 5 6 Z "
            "M 1 1 2 2 A 3",
            "left": 5,
            "top": 5,
            "strokeWidth": 6,
            "originX": "left",
            "originY": "top",
        },
        {
            "type": "path",
            "path": [["M", 0, 0], ["L", 3, 3]],
            "strokeWidth": 2,
            "left": 0,
            "top": 0,
            "globalCompositeOperation": "source-over",
        },
        {
            "type": "group",
            "objects": [
                {
                    "type": "path",
                    "path": [["M", 2, 2], ["Q", 4, 9, 6, 2]],
                    "strokeWidth": 3,
                    "left": 1,
                    "top": 1,
                }
            ],
            "left": 0,
            "top": 0,
            "width": 10,
            "height": 10,
        },
        {"type": "weird"},
    ],
}

RV_SHAPES = [
    {
        "frame": 1,
        "type": "pen",
        "name": "p",
        "properties": {
            "points": [[-0.2, 0.1], [-0.1, 0.15], [0, 0.1], [0.1, 0.05]],
            "width": [0.003, 0.004, 0.005, 0.002],
            "color": COLOR,
            "uuid": "pen-1",
        },
    },
    {
        "frame": 1,
        "type": "ellipse",
        "name": "e",
        "properties": {
            "min": [[-0.3, -0.2]],
            "max": [[0.1, 0.2]],
            "borderWidth": 0.004,
            "borderColor": COLOR,
            "innerColor": [[0, 0, 0, 0]],
        },
    },
    {
        "frame": 1,
        "type": "pen",
        "name": "er",
        "properties": {
            "points": [[-0.15, 0.12], [0, 0.12]],
            "width": 0.01,
            "color": [[1, 1, 1, 1]],
            "mode": 1,
        },
    },
    {
        "frame": 1,
        "type": "rect",
        "name": "r",
        "properties": {
            "min": [[0.2, 0.1]],
            "max": [[0.4, 0.3]],
            "borderWidth": 0.002,
            "borderColor": COLOR,
            "innerColor": [[1, 0, 0, 0.5]],
        },
    },
    {
        "frame": 2,
        "type": "arrow",
        "name": "a",
        "properties": {
            "startPos": [[0, 0]],
            "endPos": [[0.2, 0.2]],
            "thickness": 0.003,
            "borderWidth": 0.003,
            "borderColor": COLOR,
        },
    },
    {
        "frame": 2,
        "type": "text",
        "name": "t",
        "properties": {
            "position": [-0.1, 0.1],
            "color": COLOR,
            "text": "Hi\nthere\x01",
            "fontSize": 0.05,
            "fontWeight": "bold",
            "fontStyle": "italic",
            "textDecoration": "underline",
        },
    },
    {
        "frame": 2,
        "type": "text",
        "name": "t0",
        "properties": {
            "position": [0, 0],
            "color": COLOR,
            "text": "  ",
            "size": 0.004,
            "scale": 1.0,
        },
    },
    {
        "frame": 3,
        "type": "line",
        "name": "l",
        "properties": {
            "startPos": [[0, 0]],
            "endPos": [[0.1, 0.1]],
            "borderWidth": 0.01,
            "borderColor": COLOR,
        },
    },
    {"frame": 3, "type": "blob", "name": "b", "properties": {}},
    {
        "frame": 4,
        "type": "rect",
        "name": "gone",
        "properties": {"softDeleted": 1, "min": [[0, 0]], "max": [[1, 1]]},
    },
]

KITSU_RECORDS = [
    {
        "time": 0.48,
        "frame": 13,
        "drawing": {
            "objects": [
                {
                    "id": "c1",
                    "type": "circle",
                    "left": 100,
                    "top": 120,
                    "radius": 50,
                    "width": 100,
                    "height": 100,
                    "scaleX": 1.5,
                    "scaleY": 0.5,
                    "strokeWidth": 4,
                    "strokeUniform": True,
                    "stroke": [255, 0, 0, 255],
                    "fill": "transparent",
                    "opacity": 0.5,
                    "angle": 30,
                    "canvasWidth": 960,
                    "canvasHeight": 540,
                    "eraser": ERASER_GROUP,
                },
                {
                    "id": "e1",
                    "type": "ellipse",
                    "left": 200,
                    "top": 120,
                    "rx": 30,
                    "ry": 20,
                    "strokeWidth": 2,
                    "stroke": "#00ff00",
                    "fill": "#112233",
                    "originX": "center",
                    "originY": "center",
                },
                {
                    "id": "r1",
                    "type": "rect",
                    "left": 10,
                    "top": 20,
                    "width": 100,
                    "height": 50,
                    "rx": 4,
                    "ry": 4,
                    "scaleX": 2,
                    "strokeWidth": 3,
                    "stroke": "blue",
                    "fill": None,
                },
                {
                    "id": "a1",
                    "type": "arrow",
                    "left": 50,
                    "top": 60,
                    "width": 100,
                    "height": 40,
                    "x1": 10,
                    "y1": 50,
                    "x2": -90,
                    "y2": 10,
                    "flipX": True,
                    "scaleY": 2,
                    "strokeWidth": 5,
                    "stroke": "#ff0000",
                    "angle": 15,
                },
                {
                    "id": "a2",
                    "type": "arrow",
                    "x1": 1,
                    "y1": 2,
                    "x2": 3,
                    "y2": 4,
                    "strokeWidth": 5,
                    "stroke": "#ff0000",
                },
                {
                    "id": "t1",
                    "type": "textbox",
                    "left": 30,
                    "top": 40,
                    "text": "A\nB",
                    "fontSize": 20,
                    "fill": "#ffffff",
                    "textAlign": "center",
                    "angle": 10,
                    "fontWeight": 700,
                    "fontStyle": "oblique",
                    "underline": True,
                    "scaleX": 2,
                    "scaleY": 3,
                    "width": 80,
                    "strokeWidth": 1,
                },
                {
                    "id": "s1",
                    "type": "PSStroke",
                    "strokeWidth": 6,
                    "stroke": "#ffffff",
                    "strokePoints": [
                        {"x": 1, "y": 2, "pressure": 0.5},
                        {"x": 30, "y": 40},
                        {"x": 30, "y": 40},
                    ],
                },
                {
                    "id": "l1",
                    "type": "line",
                    "left": 1,
                    "top": 2,
                    "width": 3,
                    "height": 4,
                    "stroke": "#000000",
                },
                {"id": "u1", "type": "polygon"},
            ]
        },
    },
    {
        "time": 1.0,
        "drawing": {
            "objects": [
                {
                    "id": "r2",
                    "type": "rect",
                    "left": 1,
                    "top": 1,
                    "width": 2,
                    "height": 2,
                    "stroke": 12345,
                }
            ]
        },
    },
    {
        "drawing": {
            "objects": [
                {
                    "id": "r3",
                    "type": "rect",
                    "left": 1,
                    "top": 1,
                    "width": 2,
                    "height": 2,
                    "stroke": "#123456",
                }
            ]
        }
    },
]

STATUSES = [
    {
        "id": "wfa",
        "name": "Waiting",
        "short_name": "wfa",
        "color": "#f00",
        "is_feedback_request": True,
        "priority": 2,
    },
    {
        "id": "wip",
        "name": "Work",
        "short_name": "wip",
        "color": "not-a-color",
        "priority": 1,
        "is_artist_allowed": False,
    },
    {"id": "old", "name": "Old", "short_name": "old", "archived": True},
]
USER = {"id": "pp", "role": "user", "first_name": "A", "last_name": "B"}
TASK = {
    "id": "t1",
    "entity_id": "e1",
    "entity_name": "SH10",
    "task_type_id": "tt",
    "task_type_name": "Anim",
    "task_status_id": "wfa",
    "task_status_name": "Waiting",
    "task_status_short_name": "wfa",
    "project_id": "p1",
}
FULL_TASK = {
    "id": "t1",
    "task_type": {"id": "tt", "name": "Anim"},
    "task_status": {"id": "wfa", "name": "Waiting", "short_name": "wfa"},
    "entity": {"id": "e1", "name": "SH10"},
    "project": {"id": "p1"},
    "sequence": {"name": "SQ1"},
}
PLAYLIST = {
    "id": "pl1",
    "name": "Dailies <b>",
    "task_type_id": "tt",
    "for_client": True,
    "shots": [
        {"entity_id": "e1", "preview_file_id": "pf1"},
        {"entity_id": "e2"},
        {"entity_id": "e3", "preview_file_id": "bad"},
    ],
}
COMMENTS = [
    {
        "created_at": "2026-01-02T00:00:00Z",
        "person": {"first_name": "C"},
        "text": "hello",
        "task_status_id": "wfa",
    },
    {"text": "x", "task_status": {"id": "zz", "name": "Z"}},
]

calls = []
props = {}
fail = set()


def dump(label, value):
    print(f"== {label}\n" + json.dumps(value, indent=0, default=str))


def run_converter():
    """
    Convert in the two directions and return the Kitsu records of the preview.
    """
    ac._time.time = lambda: 1700000000.0
    counter = itertools.count()
    ac.uuid.uuid4 = lambda: ac.uuid.UUID(int=next(counter))

    first = ac.convertToKitsu(
        copy.deepcopy(RV_SHAPES), WIDTH, HEIGHT, fps=25, author="me"
    )
    dump("k1", first)
    shapes = ac.convertToORV(first, WIDTH, HEIGHT, fps=25, frame_base=1)
    dump("s2", shapes)
    unchanged = ac.convertToKitsu(
        copy.deepcopy(shapes),
        WIDTH,
        HEIGHT,
        fps=25,
        author="me",
        reference_records=first,
    )
    dump("k2", unchanged)
    dump("split unchanged", ac.split_annotation_changes(unchanged, first))

    erased = copy.deepcopy(shapes)
    erased.append(
        {
            "frame": 1,
            "type": "pen",
            "name": "new",
            "properties": {
                "points": [[-0.3, 0], [0.3, 0]],
                "width": 0.01,
                "color": [[1, 1, 1, 1]],
                "mode": 1,
            },
        }
    )
    changed = ac.convertToKitsu(
        erased, WIDTH, HEIGHT, fps=25, author="me", reference_records=first
    )
    dump("k3", changed)
    dump("split eraser", ac.split_annotation_changes(changed, first))

    loaded = ac.convertToORV(
        KITSU_RECORDS,
        WIDTH,
        HEIGHT,
        canvas_width=1280,
        fps=24,
        default_frame=7,
        frame_base=1,
        frame_offset=2,
    )
    dump("s4", loaded)
    back = ac.convertToKitsu(
        [s for s in copy.deepcopy(loaded) if s["type"] != "line"],
        WIDTH,
        HEIGHT,
        fps=24,
        author="me",
        reference_records=KITSU_RECORDS,
    )
    dump("k4", back)
    dump("split hand", ac.split_annotation_changes(back, KITSU_RECORDS))

    print("time", ac.frame_to_time(34, 30), ac.time_to_frame(1.0989, 30))
    for bad_fps in (0, -1):
        try:
            ac.kitsu_frame_duration(bad_fps)
        except ValueError as exc:
            print("err", exc)
    return first


def recorded(name):
    def call(*args, **kwargs):
        calls.append([name, copy.deepcopy(args), copy.deepcopy(kwargs)])

    return call


def can_fail(key, value):
    """
    Return a stub that records its call. It raises when `fail` contains `key`.
    """

    def call(*args, **kwargs):
        calls.append([key, str(args)[:120], sorted(kwargs)])
        if key in fail:
            raise RuntimeError(f"boom {key}")
        result = value(*args, **kwargs) if callable(value) else value
        return copy.deepcopy(result)

    return call


def property_setter(ptype):
    def call(name, values, *_args):
        calls.append(["set", name, ptype, list(values)])
        props[name] = (ptype, list(values))

    return call


def install_rv_stub():
    commands = types.ModuleType("rv.commands")
    commands.FloatType, commands.IntType, commands.StringType = 1, 2, 3
    commands.propertyExists = lambda name: name in props
    commands.newProperty = lambda name, ptype, width: calls.append(
        ["new", name, ptype, width]
    )
    commands.setFloatProperty = property_setter(1)
    commands.setIntProperty = property_setter(2)
    commands.setStringProperty = property_setter(3)
    commands.getFloatProperty = lambda name: props[name][1]
    commands.getIntProperty = commands.getFloatProperty
    commands.getStringProperty = commands.getFloatProperty
    commands.propertyInfo = lambda name: {"type": props[name][0]}
    commands.properties = lambda node: sorted(
        key for key in props if key.startswith(f"{node}.")
    )
    commands.frame = lambda: 1
    commands.setFPS = recorded("setFPS")
    commands.redraw = can_fail("redraw", None)
    commands.addSourceVerbose = lambda paths: "src1"
    commands.nodeGroup = lambda node: (
        "group1" if node in ("src1", "group1_paint") else "defaultSequence"
    )
    commands.nodeType = lambda node: (
        "RVSourceGroup" if node == "group1" else "RVSequenceGroup"
    )
    commands.nodesOfType = lambda node_type: [
        "defaultSequence_p_group1",
        "group1_paint",
        "other_paint",
    ]
    commands.nodeRangeInfo = lambda node: {"start": 5}
    commands.sourcesAtFrame = lambda frame: ["src1"]

    class MinorMode:
        def init(self, *args, **_kwargs):
            calls.append(["mode-init", args[0]])

    rvtypes = types.ModuleType("rv.rvtypes")
    rvtypes.MinorMode = MinorMode
    qtutils = types.ModuleType("rv.qtutils")
    rv_module = types.ModuleType("rv")
    rv_module.commands = commands
    rv_module.rvtypes = rvtypes
    rv_module.qtutils = qtutils
    sys.modules.update(
        {
            "rv": rv_module,
            "rv.commands": commands,
            "rv.rvtypes": rvtypes,
            "rv.qtutils": qtutils,
        }
    )
    return commands


class NotAuthenticatedException(Exception):
    pass


def install_gazu_stub(annotations):
    ns = types.SimpleNamespace
    preview = {
        "id": "pf1",
        "task_id": "t1",
        "revision": 3,
        "extension": "mov",
        "original_name": "clip",
        "width": WIDTH,
        "height": HEIGHT,
        "person_id": "pp",
        "created_at": "2026-01-02T03:04:05Z",
        "annotations": copy.deepcopy(annotations),
    }
    old_preview = {
        **preview,
        "id": "pf2",
        "revision": 1,
        "extension": "png",
        "width": None,
        "height": None,
        "annotations": [],
    }

    def get_preview_file(preview_id):
        if preview_id == "bad":
            raise RuntimeError("no such preview")
        return preview

    def all_playlists(project, page=1):
        if page > 1:
            return []
        return [
            {
                "id": "pl1",
                "name": "Dailies <b>",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {"id": "pl2", "name": "Empty", "shots": []},
        ]

    def download(preview_file, path, progress_callback=None):
        calls.append(["dl", os.path.basename(path)])
        if progress_callback:
            progress_callback(5, 10)

    def update_annotations(preview_file, additions, updates, deletions):
        calls.append(["push", preview_file, additions, updates, deletions])
        return {"annotations": additions + updates}

    gazu = types.ModuleType("gazu")
    exception = types.ModuleType("gazu.exception")
    exception.NotAuthenticatedException = NotAuthenticatedException
    gazu.exception = exception
    gazu.client = ns(
        default_client=ns(
            tokens={"access_token": "A", "refresh_token": "R"},
            automatic_refresh_token=False,
        ),
        get_host=lambda: "http://h/api",
        make_auth_header=lambda: {"Authorization": "x"},
        set_tokens=recorded("set_tokens"),
        get_current_user=can_fail("cur", USER),
        fetch_all=can_fail(
            "fetch_all", [{"entity_id": "e1", "task_type_id": "tt"}]
        ),
    )
    gazu.set_host = recorded("set_host")
    gazu.log_out = can_fail("log_out", {})
    gazu.refresh_token = recorded("refresh")
    gazu.log_in = can_fail("log_in", {"user": USER})
    gazu.user = ns(
        all_open_projects=can_fail("projects", [{"id": "p1", "name": "Prod"}])
    )
    gazu.project = ns(
        get_project=can_fail(
            "project",
            {
                "id": "p1",
                "name": "Prod",
                "fps": "25",
                "task_statuses": ["wfa", "wip"],
            },
        )
    )
    gazu.playlist = ns(
        all_playlists_for_project=can_fail("pls", all_playlists),
        get_playlist=can_fail("pl", PLAYLIST),
    )
    gazu.files = ns(
        get_preview_file=can_fail("pf", get_preview_file),
        get_all_preview_files_for_task=can_fail("pfs", [old_preview, preview]),
        download_preview_file=download,
        update_preview_annotations=update_annotations,
    )
    gazu.task = ns(
        all_task_statuses=can_fail("sts", STATUSES),
        all_task_types=can_fail("tts", [{"id": "tt", "name": "Anim"}]),
        all_tasks_for_person=can_fail("tasks", [TASK]),
        get_task=can_fail("task", FULL_TASK),
        get_task_status=can_fail("st", STATUSES[0]),
        all_comments_for_task=can_fail("comments", COMMENTS),
        add_comment=can_fail(
            "add_comment",
            lambda task, status, comment, person: {
                "task_status_id": status["id"]
            },
        ),
    )
    gazu.entity = ns(
        get_entity=can_fail("entity", {"id": "e1", "name": "SH10", "data": {}})
    )
    gazu.person = ns(
        get_person=can_fail(
            "person", {"first_name": "Art", "last_name": "<i>Ist"}
        )
    )
    sys.modules["gazu"] = gazu
    sys.modules["gazu.exception"] = exception

    def get_thumbnail(url, headers=None, timeout=None):
        calls.append(["thumb", url, headers, timeout])
        return ns(ok=False, content=b"")

    requests = types.ModuleType("requests")
    requests.get = get_thumbnail
    sys.modules["requests"] = requests
    # None makes "import keyring" raise ImportError.
    sys.modules["keyring"] = None
    return gazu


def table_state(table):
    rows = []
    for row in range(table.rowCount()):
        cells = []
        for column in range(table.columnCount()):
            item = table.item(row, column)
            cells.append(
                None
                if item is None
                else [item.text(), item.toolTip(), item.font().bold()]
            )
        rows.append([table.isRowHidden(row), cells])
    return rows


def show_state(panel, label):
    from PySide6 import QtWidgets

    combo = panel.status_combo
    comments = panel.comments_list
    dump(
        f"STATE {label}",
        {
            "status": panel.status_label.text(),
            "btn": panel.login_btn.text(),
            "detail": panel.detail_label.text(),
            "playlists": table_state(panel.playlist_table),
            "plrev": table_state(panel.playlist_rev_table),
            "checks": table_state(panel.checks_table),
            "plinfo": panel.playlists_info.text(),
            "chinfo": panel.checks_info.text(),
            "comments": [
                comments.item(i).text() for i in range(comments.count())
            ],
            "combo": [combo.itemText(i) for i in range(combo.count())],
            "addbtn": panel.add_comment_btn.text(),
            "en": [
                widget.isEnabled()
                for widget in (
                    panel.download_btn,
                    panel.export_btn,
                    panel.add_comment_btn,
                    panel.status_combo,
                    panel.refresh_checks_btn,
                )
            ],
            "cursor": bool(QtWidgets.QApplication.overrideCursor()),
        },
    )
    dump(f"CALLS {label}", calls)
    calls.clear()


def install_message_boxes():
    """
    Replace the modal boxes, which block in offscreen mode, with a print.
    """
    from PySide6 import QtWidgets

    def make_box(kind):
        return staticmethod(lambda *args: print("BOX", kind, repr(args[-1])))

    for kind in ("information", "warning", "critical"):
        setattr(QtWidgets.QMessageBox, kind, make_box(kind))


def run_login(panel, app):
    panel.server_field.setText("http://h/api/")
    panel.user_field.setText("a@b.c")
    panel._on_login_clicked()
    panel.pass_field.setText("pw")
    fail.add("log_in")
    panel._on_login_clicked()
    fail.clear()
    show_state(panel, "login-fail")
    panel.pass_field.setText("pw")
    panel._on_login_clicked()
    app.processEvents()
    show_state(panel, "login")


def run_review(panel, app):
    panel.playlist_table.selectRow(0)
    app.processEvents()
    show_state(panel, "pl-selected")
    panel.playlist_rev_table.selectRow(0)
    app.processEvents()
    show_state(panel, "rev-selected")
    panel.status_combo.setCurrentIndex(panel.status_combo.findData("wip"))
    panel.comment_input.setText("note")
    panel._on_add_comment_clicked()
    show_state(panel, "comment")
    fail.add("add_comment")
    panel.comment_input.setText("n2")
    panel._on_add_comment_clicked()
    fail.clear()
    show_state(panel, "comment-fail")
    fail.add("redraw")
    panel._on_download_clicked()
    fail.clear()
    show_state(panel, "load-fail")
    panel._on_download_clicked()
    show_state(panel, "download")
    dump("props", props)


def run_export(panel, commands):
    panel._on_export_clicked()
    show_state(panel, "export-noop")
    pen = "group1_paint.pen:99:9:Me"
    commands.setStringProperty(
        "group1_paint.frame:9.order", ["pen:99:9:Me"], True
    )
    commands.setFloatProperty(f"{pen}.points", [-0.1, 0, 0.1, 0], True)
    commands.setFloatProperty(f"{pen}.color", [1, 0, 0, 1], True)
    commands.setFloatProperty(f"{pen}.width", [0.01, 0.01], True)
    calls.clear()
    panel._on_export_clicked()
    show_state(panel, "export-new")


def run_dev_tab(panel, app):
    for index, _view in enumerate(panel.DEV_VIEWS):
        panel.dev_view_combo.setCurrentIndex(index)
        for scope in (True, False):
            panel.dev_scope_check.setChecked(scope)
            view = panel.dev_view_combo.currentData()
            result = panel.model.build_dev_view(view, scope)
            print("DEV", index, scope, result)
    panel.right_tabs.setCurrentWidget(panel.dev_tab)
    app.processEvents()
    print("timer", panel._dev_timer.isActive())
    # The label ends with the clock time, so compare only the first part.
    print("devstatus", panel.dev_status_label.text().split("·")[0])
    panel.right_tabs.setCurrentIndex(0)


def run_checks_and_failures(panel, app):
    panel.source_tabs.setCurrentIndex(1)
    app.processEvents()
    show_state(panel, "checks-tab")
    panel.checks_table.selectRow(0)
    app.processEvents()
    show_state(panel, "check-selected")
    panel.show_all_playlists_check.setChecked(True)
    show_state(panel, "show-all")

    fail.add("tasks")
    panel._on_refresh_checks_clicked()
    fail.clear()
    show_state(panel, "checks-fail")

    fail.add("projects")
    panel._on_refresh_playlists_clicked()
    fail.clear()
    show_state(panel, "pl-fail")

    fail.add("comments")
    panel.checks_table.clearSelection()
    panel.checks_table.selectRow(0)
    fail.clear()
    show_state(panel, "comments-fail")

    fail.update({"sts", "tts", "project", "entity"})
    panel._on_refresh_checks_clicked()
    fail.clear()
    show_state(panel, "catalog-fail")

    panel._on_login_clicked()
    show_state(panel, "logout")


def run_model_checks(model, model_module, gazu, commands):
    print("restore ok", model.restore_session("s", "e", {"access_token": "a"}))

    def rejected():
        raise NotAuthenticatedException()

    gazu.client.get_current_user = rejected
    try:
        model.restore_session("s", "e", {"access_token": "a"})
    except model_module.SessionRejected:
        print("rejected")

    dimensions = model_module._preview_dimensions
    objects = [{"canvasWidth": "10.4", "canvasHeight": 20}]
    print(
        "dims",
        dimensions({"annotations": [{"drawing": {"objects": objects}}]}),
    )
    print("dims", dimensions({}, {"data": {"resolution": "1920x1080"}}))
    try:
        dimensions({"id": "x"})
    except model_module.DimensionsUnknownError as exc:
        print(exc)

    print(
        "nodes",
        model.rv.paint_nodes_for_source("group1"),
        model.rv._pipeline_order(commands.nodesOfType("RVPaint")),
    )
    gathered = model.rv.gather_annotations(None)
    print("gather", json.dumps(gathered))


def main():
    annotations = run_converter()
    commands = install_rv_stub()
    gazu = install_gazu_stub(annotations)

    from PySide6 import QtWidgets

    app = QtWidgets.QApplication([])
    install_message_boxes()

    import kitsu
    import kitsu_review_model

    panel = kitsu.KitsuReviewPanel()
    panel.show()
    app.processEvents()
    show_state(panel, "init")

    run_login(panel, app)
    run_review(panel, app)
    run_export(panel, commands)
    run_dev_tab(panel, app)
    run_checks_and_failures(panel, app)
    run_model_checks(panel.model, kitsu_review_model, gazu, commands)
    print(type(kitsu.createMode()).__name__)


if __name__ == "__main__":
    main()
