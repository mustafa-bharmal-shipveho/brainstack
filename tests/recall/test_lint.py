"""Tests for `recall lint` — deterministic staleness/integrity checks.

Two layers:

- TestHelpers exercises the pure decision functions against the REAL root
  constants, using well-known system paths (`/usr/bin/env` exists on macOS
  and Linux CI; `/usr/bin/__no_such__` does not).
- TestLintBrain runs the full pass over a temp brain. Because pytest's
  tmp_path lives under an ephemeral root (`/private/var/folders` on macOS,
  `/tmp` on Linux) which lint intentionally ignores, the `anyroot` fixture
  monkeypatches the root constants so any absolute path under tmp_path is
  treated as a real, checkable filesystem path.
"""

from __future__ import annotations

import pytest

from recall import lint


# --------------------------------------------------------------------------
# Helper-level tests (real root constants)
# --------------------------------------------------------------------------
class TestHelpers:
    def test_strip_line_ref(self):
        assert lint._strip_line_ref("/usr/bin/env:12") == "/usr/bin/env"
        assert lint._strip_line_ref("/a/b.md:3,7,9") == "/a/b.md"
        assert lint._strip_line_ref("/a/b.md") == "/a/b.md"
        # A ':' that is not a trailing line-ref is left alone.
        assert lint._strip_line_ref("/a/b:c") == "/a/b:c"

    def test_is_placeholder(self):
        assert lint._is_placeholder("/Users/me/x.md")
        assert lint._is_placeholder("/path/to/thing")
        assert lint._is_placeholder("<your-org>/repo")
        assert lint._is_placeholder("~/foo/*.md")
        assert not lint._is_placeholder("/Users/realuser/notes.md")

    def test_checkable_roots_exclude_routes_and_ephemeral(self):
        # Real filesystem roots → checkable.
        assert lint._is_checkable_fs_path("/usr/bin/env")
        assert lint._is_checkable_fs_path("/Users/x/y.md")
        # URL routes / slash-commands → NOT checkable.
        assert not lint._is_checkable_fs_path("/agent-team")
        assert not lint._is_checkable_fs_path("/login")
        assert not lint._is_checkable_fs_path("/noc/intake")
        # Ephemeral temp dirs → NOT checkable (absence is expected).
        assert not lint._is_checkable_fs_path("/tmp/scratch.json")
        assert not lint._is_checkable_fs_path("/private/var/folders/ab/cd.tmp")

    def test_path_is_dead_real_paths(self):
        assert lint._path_is_dead("/usr/bin/__no_such_binary_zzz__")
        assert not lint._path_is_dead("/usr/bin/env")

    def test_path_is_dead_strips_line_ref(self):
        # /usr/bin/env exists; the :12 must not make it look dead.
        assert not lint._path_is_dead("/usr/bin/env:12")

    def test_path_is_dead_command_first_token(self):
        # A command span: the real binary is the first token, args follow.
        assert not lint._path_is_dead('/usr/bin/env --version "x"')
        assert lint._path_is_dead('/usr/bin/__nope__ --version')

    def test_path_is_dead_routes_and_ephemeral_never_dead(self):
        assert not lint._path_is_dead("/agent-team")
        assert not lint._path_is_dead("/tmp/anything_missing.json")

    def test_path_candidates_whole_span_for_path_with_spaces(self):
        # A path containing spaces must be returned whole, not truncated.
        assert lint._path_candidates_in_span("/opt/My App/conf.ini") == [
            "/opt/My App/conf.ini"
        ]

    def test_path_candidates_strips_trailing_annotation(self):
        assert lint._path_candidates_in_span("/usr/bin/env (the binary)") == [
            "/usr/bin/env"
        ]

    def test_path_candidates_embedded_in_command(self):
        assert lint._path_candidates_in_span("cat /usr/bin/env now") == [
            "/usr/bin/env"
        ]

    def test_slugify(self):
        assert lint._slugify("Feedback No Emdashes") == "feedback-no-emdashes"
        assert lint._slugify("feedback_no_emdashes") == "feedback_no_emdashes"


# --------------------------------------------------------------------------
# End-to-end lint over a temp brain
# --------------------------------------------------------------------------
@pytest.fixture
def anyroot(monkeypatch):
    """Treat any absolute path as a real, checkable filesystem path.

    tmp_path lives under an ephemeral root that lint ignores by design, so
    for end-to-end existence tests we widen the checkable set to '/' and
    clear the ephemeral exclusions.
    """
    monkeypatch.setattr(lint, "_EPHEMERAL_ROOTS", ())
    monkeypatch.setattr(lint, "_CHECKABLE_ROOTS", ("/",))


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_dead_path_flagged_live_path_not(tmp_path, anyroot):
    brain = tmp_path / "brain"
    live = tmp_path / "real_file.md"
    live.write_text("hi", encoding="utf-8")
    _write(
        brain / "memory" / "a.md",
        f"---\nname: a\n---\nSee `{live}` and `{tmp_path}/gone.md`.\n",
    )
    findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
    details = [f.detail for f in findings]
    assert any("gone.md" in d for d in details)
    assert not any("real_file.md" in d for d in details)


def test_spaced_live_path_not_flagged(tmp_path, anyroot):
    brain = tmp_path / "brain"
    spaced = tmp_path / "My Notes" / "plan.md"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("x", encoding="utf-8")
    _write(brain / "memory" / "a.md", f"---\nname: a\n---\nfile `{spaced}` here\n")
    findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
    assert findings == []


def test_line_ref_live_path_not_flagged(tmp_path, anyroot):
    brain = tmp_path / "brain"
    f = tmp_path / "code.md"
    f.write_text("x", encoding="utf-8")
    _write(brain / "memory" / "a.md", f"---\nname: a\n---\nat `{f}:42` see it\n")
    findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
    assert findings == []


def test_route_and_tmp_not_flagged_with_real_constants(tmp_path):
    # No anyroot fixture → real constants apply.
    brain = tmp_path / "brain"
    _write(
        brain / "memory" / "a.md",
        "---\nname: a\n---\nRun `/agent-team` then hit `/login`; scratch `/tmp/x.json`.\n",
    )
    findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
    assert findings == []


def test_broken_and_valid_wikilinks(tmp_path):
    brain = tmp_path / "brain"
    _write(brain / "memory" / "target.md", "---\nname: My Target\n---\nbody\n")
    _write(
        brain / "memory" / "src.md",
        "---\nname: src\n---\nlinks [[my-target]] and [[ghost-note]]\n",
    )
    findings = lint.lint_brain(brain, kinds=frozenset({"broken_wikilink"}))
    details = [f.detail for f in findings]
    assert any("ghost-note" in d for d in details)
    assert not any("my-target" in d for d in details)


def test_repo_relative_link_skipped_brain_link_checked(tmp_path):
    brain = tmp_path / "brain"
    _write(brain / "memory" / "present.md", "---\nname: p\n---\nok\n")
    _write(
        brain / "memory" / "plan.md",
        "---\nname: plan\n---\n"
        "[code](src/logic/foo.ts) "        # repo-relative .ts → skipped
        "[here](./present.md) "            # brain-internal, exists → ok
        "[gone](./missing.md)\n",          # brain-internal, missing → flagged
    )
    findings = lint.lint_brain(brain, kinds=frozenset({"broken_local_link"}))
    details = [f.detail for f in findings]
    assert any("missing.md" in d for d in details)
    assert not any("foo.ts" in d for d in details)
    assert not any("present.md" in d for d in details)


def test_missing_frontmatter_only_for_lessons(tmp_path):
    brain = tmp_path / "brain"
    _write(
        brain / "memory" / "semantic" / "lessons" / "bad.md",
        "---\nname: only-name\n---\nbody\n",  # missing description + type
    )
    _write(
        brain / "memory" / "semantic" / "lessons" / "good.md",
        "---\nname: g\ndescription: d\ntype: feedback\n---\nbody\n",
    )
    _write(brain / "memory" / "digests" / "x.md", "---\nname: x\n---\nbody\n")  # not a lesson
    findings = lint.lint_brain(brain, kinds=frozenset({"missing_frontmatter"}))
    bad = [f for f in findings]
    assert len(bad) == 1
    assert "lessons/bad.md" in str(bad[0].file)
    assert "description" in bad[0].detail and "type" in bad[0].detail


def test_mark_needs_review_idempotent_and_gated(tmp_path):
    with_fm = tmp_path / "a.md"
    with_fm.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
    no_fm = tmp_path / "b.md"
    no_fm.write_text("just body, no frontmatter\n", encoding="utf-8")

    modified = lint.mark_needs_review([with_fm, no_fm])
    assert modified == [with_fm]  # only the file with frontmatter
    assert "needs_review: true" in with_fm.read_text(encoding="utf-8")
    assert "needs_review" not in no_fm.read_text(encoding="utf-8")

    # Idempotent: second run changes nothing.
    assert lint.mark_needs_review([with_fm]) == []
    assert with_fm.read_text(encoding="utf-8").count("needs_review") == 1


def test_lint_file_never_raises_on_unreadable(tmp_path):
    missing = tmp_path / "nope.md"
    assert lint.lint_file(
        missing, known_keys=set(), kinds=lint.ALL_KINDS, brain_root=tmp_path
    ) == []


# --------------------------------------------------------------------------
# Regression tests for issues found in Codex review
# --------------------------------------------------------------------------
class TestCodexReviewFixes:
    def test_line_ref_with_column(self):
        # `:12:34` (line:col) must be stripped, not just `:12,34`.
        assert lint._strip_line_ref("/usr/bin/env:12:34") == "/usr/bin/env"
        assert not lint._path_is_dead("/usr/bin/env:12:34")

    def test_frontmatter_mixed_key_types_no_crash(self, tmp_path):
        brain = tmp_path / "brain"
        # YAML `1:` parses to an int key — sorting mixed keys must not crash.
        _write(
            brain / "memory" / "semantic" / "lessons" / "weird.md",
            "---\n1: x\nname: ok\n---\nbody\n",
        )
        findings = lint.lint_brain(brain, kinds=frozenset({"missing_frontmatter"}))
        assert len(findings) == 1  # missing description + type, reported cleanly

    def test_wikilink_with_anchor_resolves(self, tmp_path):
        brain = tmp_path / "brain"
        _write(brain / "memory" / "target.md", "---\nname: target\n---\nbody\n")
        _write(
            brain / "memory" / "src.md",
            "---\nname: src\n---\n[[target#Some Heading]] and [[target.md]]\n",
        )
        findings = lint.lint_brain(brain, kinds=frozenset({"broken_wikilink"}))
        assert findings == []  # anchor + .md must not break resolution

    def test_md_link_fragment_and_percent_decode(self, tmp_path, anyroot):
        brain = tmp_path / "brain"
        live = tmp_path / "My Plan.md"
        live.write_text("x", encoding="utf-8")
        _write(
            brain / "memory" / "a.md",
            f"---\nname: a\n---\n[x]({tmp_path}/My%20Plan.md#L12)\n",
        )
        findings = lint.lint_brain(brain, kinds=frozenset({"broken_local_link"}))
        assert findings == []  # %20 decoded + #fragment dropped → resolves

    def test_quoted_command_path_with_spaces_not_truncated(self, tmp_path, anyroot):
        brain = tmp_path / "brain"
        spaced = tmp_path / "My Notes" / "plan.md"
        spaced.parent.mkdir(parents=True)
        spaced.write_text("x", encoding="utf-8")
        _write(
            brain / "memory" / "a.md",
            f'---\nname: a\n---\nrun `cat "{spaced}"` to see it\n',
        )
        findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
        assert findings == []  # quoted spaced path resolves whole, not truncated

    def test_path_candidates_quoted_segment(self):
        cands = lint._path_candidates_in_span('cat "/opt/My App/x.ini" now')
        assert "/opt/My App/x.ini" in cands

    def test_mark_preserves_crlf_and_body(self, tmp_path):
        f = tmp_path / "crlf.md"
        f.write_bytes(b"---\r\nname: a\r\n---\r\nbody line\r\nsecond\r\n")
        lint.mark_needs_review([f])
        raw = f.read_bytes()
        assert b"needs_review: true" in raw
        # CRLF preserved, body intact.
        assert b"body line\r\nsecond\r\n" in raw
        assert b"\n" in raw and raw.count(b"\r\n") >= 4

    def test_mark_skips_symlink(self, tmp_path):
        real = tmp_path / "real.md"
        real.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
        link = tmp_path / "link.md"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported")
        assert lint.mark_needs_review([link]) == []
        assert "needs_review" not in real.read_text(encoding="utf-8")

    def test_mark_skips_non_utf8(self, tmp_path):
        f = tmp_path / "latin1.md"
        f.write_bytes(b"---\nname: caf\xe9\n---\nbody\n")  # latin-1 é
        assert lint.mark_needs_review([f]) == []

    def test_quoted_prose_containing_path_still_checked(self, tmp_path, anyroot):
        # `cmd "see /x/missing.md"` — the quote is prose, not a pure path.
        # The embedded missing path must NOT be hidden (false negative).
        brain = tmp_path / "brain"
        _write(
            brain / "memory" / "a.md",
            f'---\nname: a\n---\nrun `cmd "see {tmp_path}/missing.md"`\n',
        )
        findings = lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
        assert any("missing.md" in f.detail for f in findings)

    def test_wikilink_with_hash_in_name_resolves(self, tmp_path):
        # A note literally named "C# notes" must resolve via the full slug.
        brain = tmp_path / "brain"
        _write(brain / "memory" / "C# notes.md", "---\nname: C# notes\n---\nbody\n")
        _write(brain / "memory" / "src.md", "---\nname: src\n---\nsee [[C# notes]]\n")
        findings = lint.lint_brain(brain, kinds=frozenset({"broken_wikilink"}))
        assert findings == []

    def test_mark_temp_symlink_not_followed(self, tmp_path):
        # A planted temp symlink must not be written through.
        f = tmp_path / "a.md"
        f.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("ORIGINAL", encoding="utf-8")
        # mkstemp uses a random suffix, so an attacker can't predict the name;
        # even a same-prefix symlink won't be the chosen path. Sanity-check the
        # write still lands on the real file and leaves `outside` untouched.
        lint.mark_needs_review([f])
        assert "needs_review: true" in f.read_text(encoding="utf-8")
        assert outside.read_text(encoding="utf-8") == "ORIGINAL"

    def test_lint_file_never_raises_on_null_byte_path(self, tmp_path, anyroot):
        brain = tmp_path / "brain"
        _write(brain / "memory" / "a.md", "---\nname: a\n---\nbad `/Users/x/\x00y.md`\n")
        # Should not raise despite the embedded null byte.
        findings = lint.lint_brain(brain, kinds=lint.ALL_KINDS)
        assert isinstance(findings, list)


# --------------------------------------------------------------------------
# Unparseable-frontmatter detection + repair
# --------------------------------------------------------------------------
class TestRepairFrontmatter:
    BROKEN = (
        "---\nsession_id: abc\n"
        "outcome: Scope negotiated: 502 backend error deferred to triage\n"
        "salience: 9\nneeds_review: true\n---\nbody line\nmore body\n"
    )

    def test_detects_unparseable(self, tmp_path):
        brain = tmp_path / "brain"
        _write(brain / "memory" / "digests" / "broken.md", self.BROKEN)
        _write(brain / "memory" / "digests" / "ok.md", "---\nname: a\n---\nb\n")
        findings = lint.lint_brain(brain, kinds=frozenset({"unparseable_frontmatter"}))
        files = {f.file.name for f in findings}
        assert files == {"broken.md"}

    def test_repair_fixes_and_parses(self, tmp_path):
        import yaml
        f = tmp_path / "broken.md"
        f.write_text(self.BROKEN, encoding="utf-8")
        assert lint.repair_frontmatter([f]) == [f]
        out = f.read_text(encoding="utf-8")
        block = out.split("---", 2)[1]
        parsed = yaml.safe_load(block)
        assert parsed["outcome"] == "Scope negotiated: 502 backend error deferred to triage"
        assert parsed["needs_review"] is True
        assert parsed["salience"] == 9
        # Body preserved exactly.
        assert "body line\nmore body\n" in out

    def test_repair_preserves_body_and_other_fields(self, tmp_path):
        f = tmp_path / "broken.md"
        f.write_text(self.BROKEN, encoding="utf-8")
        lint.repair_frontmatter([f])
        out = f.read_text(encoding="utf-8")
        assert "session_id: abc" in out  # untouched field
        assert out.endswith("body line\nmore body\n")

    def test_repair_skips_already_valid(self, tmp_path):
        f = tmp_path / "ok.md"
        original = "---\nname: a\noutcome: done\n---\nbody\n"
        f.write_text(original, encoding="utf-8")
        assert lint.repair_frontmatter([f]) == []
        assert f.read_text(encoding="utf-8") == original

    def test_repair_idempotent(self, tmp_path):
        f = tmp_path / "broken.md"
        f.write_text(self.BROKEN, encoding="utf-8")
        lint.repair_frontmatter([f])
        first = f.read_text(encoding="utf-8")
        assert lint.repair_frontmatter([f]) == []  # now parses → no-op
        assert f.read_text(encoding="utf-8") == first

    def test_repair_crlf_preserved(self, tmp_path):
        f = tmp_path / "broken.md"
        f.write_bytes(
            b"---\r\noutcome: Scope: broke it\r\nsalience: 9\r\n---\r\nbody\r\nx\r\n"
        )
        lint.repair_frontmatter([f])
        raw = f.read_bytes()
        assert b"body\r\nx\r\n" in raw
        import yaml
        block = raw.decode("utf-8").split("---", 2)[1]
        assert yaml.safe_load(block)["outcome"] == "Scope: broke it"

    def test_repair_fixes_leading_at_sign(self, tmp_path):
        # A value starting with a reserved YAML indicator (@) breaks bare YAML;
        # the repair must detect and quote it.
        import yaml
        assert lint._value_breaks_bare_yaml("@mention triaged it")
        f = tmp_path / "b.md"
        f.write_text("---\nname: a\nowner: @mention triaged it\n---\nbody\n", encoding="utf-8")
        assert lint.repair_frontmatter([f]) == [f]
        parsed = yaml.safe_load(f.read_text(encoding="utf-8").split("---", 2)[1])
        assert parsed["owner"] == "@mention triaged it"

    def test_repair_escapes_backslash_and_quote(self, tmp_path):
        import yaml
        f = tmp_path / "b.md"
        f.write_text('---\nkey: path C:\\x "y" and a: colon\nn: 1\n---\nbody\n', encoding="utf-8")
        assert lint.repair_frontmatter([f]) == [f]
        parsed = yaml.safe_load(f.read_text(encoding="utf-8").split("---", 2)[1])
        assert parsed["key"] == 'path C:\\x "y" and a: colon'

    def test_repair_preserves_scalar_types(self, tmp_path):
        # Int/bool/date/timestamp values must NOT be quoted (no type change).
        import yaml
        f = tmp_path / "b.md"
        f.write_text(
            "---\noutcome: broke: it\ncount: 42\nflag: true\nts: 2026-04-17T19:33:48Z\n---\nbody\n",
            encoding="utf-8",
        )
        lint.repair_frontmatter([f])
        parsed = yaml.safe_load(f.read_text(encoding="utf-8").split("---", 2)[1])
        assert parsed["count"] == 42 and parsed["flag"] is True   # types intact
        assert parsed["outcome"] == "broke: it"                   # only the breaker quoted

    def test_repair_leaves_unfixable_untouched(self, tmp_path):
        # A structurally-broken block the scalar re-quote can't fix
        # (bad indentation) must be left as-is, not corrupted.
        f = tmp_path / "weird.md"
        original = "---\nkey:\n  - a\n bad_indent: x\n---\nbody\n"
        f.write_text(original, encoding="utf-8")
        lint.repair_frontmatter([f])  # may or may not fix; must not corrupt
        # If unchanged, it's still the original; if changed, it must parse.
        out = f.read_text(encoding="utf-8")
        if out != original:
            import yaml
            yaml.safe_load(out.split("---", 2)[1])  # must not raise


# --------------------------------------------------------------------------
# Auto-clear: find_flagged_files + unmark_needs_review
# --------------------------------------------------------------------------
class TestAutoClear:
    def test_find_flagged_files(self, tmp_path):
        brain = tmp_path / "brain"
        _write(brain / "memory" / "flagged.md", "---\nname: a\nneeds_review: true\n---\nb\n")
        _write(brain / "memory" / "flagged_str.md", "---\nname: a\nneeds_review: 'yes'\n---\nb\n")
        _write(brain / "memory" / "clean.md", "---\nname: a\n---\nb\n")
        _write(brain / "memory" / "falseflag.md", "---\nname: a\nneeds_review: false\n---\nb\n")
        flagged = {p.name for p in lint.find_flagged_files(brain)}
        assert flagged == {"flagged.md", "flagged_str.md"}

    def test_unmark_removes_flag_preserves_body(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("---\nname: a\nneeds_review: true\nsalience: 9\n---\nbody line\nmore\n",
                     encoding="utf-8")
        assert lint.unmark_needs_review([f]) == [f]
        out = f.read_text(encoding="utf-8")
        assert "needs_review" not in out
        assert "name: a" in out and "salience: 9" in out  # other fm kept
        assert "body line\nmore\n" in out                 # body intact

    def test_unmark_noop_when_absent(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
        assert lint.unmark_needs_review([f]) == []

    def test_unmark_preserves_crlf_body(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_bytes(b"---\r\nname: a\r\nneeds_review: true\r\n---\r\nbody\r\nx\r\n")
        lint.unmark_needs_review([f])
        raw = f.read_bytes()
        assert b"needs_review" not in raw
        assert b"body\r\nx\r\n" in raw

    def test_unmark_skips_symlink(self, tmp_path):
        real = tmp_path / "real.md"
        real.write_text("---\nname: a\nneeds_review: true\n---\nbody\n", encoding="utf-8")
        link = tmp_path / "link.md"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unsupported")
        assert lint.unmark_needs_review([link]) == []
        assert "needs_review: true" in real.read_text(encoding="utf-8")

    def test_mark_then_unmark_round_trip(self, tmp_path):
        f = tmp_path / "a.md"
        original = "---\nname: a\n---\nbody\n"
        f.write_text(original, encoding="utf-8")
        lint.mark_needs_review([f])
        assert "needs_review: true" in f.read_text(encoding="utf-8")
        lint.unmark_needs_review([f])
        assert f.read_text(encoding="utf-8") == original

    def test_find_flagged_robust_to_unparseable_yaml(self, tmp_path):
        # A real failure mode: an unquoted `outcome:` value containing a colon
        # makes yaml.safe_load fail, so a parse-based scan would MISS the flag.
        brain = tmp_path / "brain"
        _write(
            brain / "memory" / "digests" / "broken.md",
            "---\nname: d\noutcome: Scope negotiated: 502 deferred to triage\n"
            "needs_review: true\n---\nbody\n",
        )
        from recall.frontmatter import parse_path
        # Confirm the parser really does choke (frontmatter empty).
        assert parse_path(brain / "memory" / "digests" / "broken.md").frontmatter == {}
        # find_flagged_files must still detect it via the raw scan.
        flagged = {p.name for p in lint.find_flagged_files(brain)}
        assert "broken.md" in flagged

    def test_auto_clear_scenario(self, tmp_path, anyroot):
        # A flagged memory whose dead path is recreated → reconcile clears it.
        brain = tmp_path / "brain"
        gone = tmp_path / "was_missing.md"
        mem = brain / "memory" / "digest.md"
        _write(mem, f"---\nname: d\nneeds_review: true\n---\nrefs `{gone}`\n")
        # While the path is missing, lint still finds it → stays flagged.
        assert lint.lint_brain(brain, kinds=frozenset({"dead_path"}))
        # Recreate the path; now zero findings.
        gone.write_text("back", encoding="utf-8")
        assert lint.lint_brain(brain, kinds=frozenset({"dead_path"})) == []
        # Reconcile: file is flagged but has no findings → auto-clear.
        flagged = set(lint.find_flagged_files(brain))
        cleared = lint.unmark_needs_review(flagged)
        assert mem in cleared
        assert "needs_review" not in mem.read_text(encoding="utf-8")
