from __future__ import annotations

from acdp_hooks.ctxids import ctx_ids_in_arguments, ctx_ids_in_text, dedupe, is_ctx_id

A = "acdp://registry.example.com/12345678-1234-4321-8123-123456781234"
B = "acdp://r.example/aaaaaaaa-bbbb-4ccc-9ddd-eeeeeeeeeeee"
C = "acdp://localhost/00000000-0000-4000-a000-000000000000"


def test_grammar() -> None:
    assert is_ctx_id(A)
    assert not is_ctx_id(A.upper())
    assert not is_ctx_id(A + "x")
    assert not is_ctx_id("acdp://r.example/12345678-1234-1234-8123-123456781234")  # not v4
    assert not is_ctx_id(42)


def test_arguments_walk_in_jcs_order_whole_strings_only() -> None:
    args = {"z": A, "b": [C, {"y": B}], "a": f"see {B}", A: "key is not taken"}
    # Keys sorted: "a" (embedded, not taken), "acdp://…" (value is not an id),
    # "b" → C then B, "z" → A.
    assert ctx_ids_in_arguments(args) == [C, B, A]


def test_arguments_given_as_json_text() -> None:
    assert ctx_ids_in_arguments(f'{{"id": "{A}"}}') == [A]
    assert ctx_ids_in_arguments("not json " + A) == []


def test_text_scan_finds_embedded_ids_with_boundaries() -> None:
    text = f"read {A}, and {B}. Also x{C} and {C}0 and {A} again"
    assert ctx_ids_in_text(text) == [A, B]
    assert ctx_ids_in_text({"content": [{"text": f"<{C}>"}]}) == [C]
    assert ctx_ids_in_text(object()) == []


def test_dedupe_caps() -> None:
    assert dedupe(["x", "y", "x", "z"], limit=2) == ["x", "y"]
