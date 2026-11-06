"""No new thread_local in product code (CPU-SCOPE-LIVE-CRASH-FIX-1).

On mingw-w64 v11 the exe's TLS callback runs a thread_local's C++ destructor after libgcc's emutls key destructor has
freed its storage. Non-trivial per-thread caches go through mlvThreadExitCache (src/processing/ThreadExitCache.h).

Grammar: the whole word thread_local anywhere in any file under platform/ or src/, against an exact allow-list of
(relative path, stripped line, reason). This does not parse C++. It does not interpret comments or literals, and
every approximation it makes fails toward reporting a line, never toward hiding one:
- Every regular file is read, whatever its suffix (.inc, .hxx, .hh, .ipp, no suffix at all), as bytes decoded as
  UTF-8 with errors=replace. A token in a non-C++ file is an over-report that an allow-list row answers. There is
  no include list. The one exception is a named skip list of binary suffixes, because reading them cost about 12 s
  of the 13 s the scan took on the real tree (r3 measurement), and none can hold C++ the compiler reads:
  .zip and .xz (compressed archives of the prebuilt FFmpeg and raw2mlv binaries under platform/qt; DLLs and
  executables are out of scope below), .png, .gif, .icns and .ico (images), .blend (a Blender scene) and .ttf
  (a font). The .fpm pixel maps are text and are read.
- Each file is searched in two views and the union is reported: the text as it is, and the text with every
  continuation removed (a backslash, optional spaces, tabs, form feeds or vertical tabs, then LF, CR LF or a lone
  CR; GCC accepts the whitespace with a warning). A token split by a continuation shows in the second view. A token
  at a line end that a continuation would glue to the next word (thread_local, backslash, newline, Foo g;) shows in
  the first. Translation reverts a continuation inside a raw string; the second view does not, but since the first
  view is searched too, that can only add a report, never remove one.
- A comment, string or raw string holding the token is reported on purpose and needs an allow-list row (or a
  reword). The guard used to strip comments; that lexer kept having corners (digit separators, raw strings,
  continuations) and each corner hid a real declaration.

Limits, stated plainly (out of scope):
- Token pasting: #define CAT(a,b) a##b, then CAT(thread_,local) Foo g; holds no token in the source.
- A macro or -D define that spells the token outside platform/ and src/ (a .pro under platform/ is read).
- UTF-16 sources: the token is not a run of UTF-8 bytes there.
- Trigraph ??/ as a backslash (off by default in GNU modes; removed in C++17).
- Generated code and DLLs: Qt6Core.dll, libgomp-1.dll and libstdc++-6.dll reference __emutls_get_address and
  nothing here reaches them.
- It cannot judge triviality on an allow-listed line: the reason is a human's claim.
- __thread, __declspec(thread) and C11 _Thread_local register no destructor; QThreadStorage is a different
  mechanism with no product use today.
- The object census (nm -u for __cxa_thread_atexit on every release .o) is the structural proof; this is only the
  source-level gate.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRECTORIES = ("platform", "src")
# Binary suffixes the scan skips; the docstring gives the reason. Never turn this into an include list.
SKIPPED_BINARY_SUFFIXES = frozenset({".zip", ".xz", ".png", ".gif", ".icns", ".ico", ".blend", ".ttf"})
TOKEN = re.compile(r"\bthread_local\b")
CONTINUATION = re.compile(r"\\[ \t\f\v]*(?:\r\n|\n|\r)")

COMMENT_TEXT = "comment text; declares nothing"
ALLOW_LIST = {
    (
        "platform/qt/AudioPlayback.cpp",
        "static thread_local bool comInitialized = false;",
    ): "trivially destructible bool; registers no destructor",
    (
        "src/processing/ThreadExitCache.h",
        "T & mlvThreadExitCache() { static thread_local T t; return t; }",
    ): "the helper's non-MinGW branch; the failing ordering is specific to MinGW",
    (
        "src/processing/ThreadExitCache.h",
        "* thread_local objects from coming back. */",
    ): COMMENT_TEXT,
    (
        "src/processing/ThreadExitCache.h",
        "// Why: on mingw-w64 v11 the exe's TLS callback runs thread_local destructors after",
    ): COMMENT_TEXT,
}


def views(text):
    """The text as it is, and the text with every continuation removed."""
    return (text, CONTINUATION.sub("", text))


def thread_local_lines(text):
    """The stripped lines, from either view, that hold the token; each distinct line once."""
    found = []
    for view in views(text):
        for line in view.splitlines():
            stripped = line.strip()
            if TOKEN.search(stripped) and stripped not in found:
                found.append(stripped)
    return found


def scan(root):
    """Every (relative path, stripped line) in the scanned product trees, from every regular file but the skip list."""
    found = []
    for directory in SCANNED_DIRECTORIES:
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and path.suffix.lower() not in SKIPPED_BINARY_SUFFIXES:
                text = path.read_bytes().decode("utf-8", errors="replace")
                relative = path.relative_to(root).as_posix()
                found.extend((relative, line) for line in thread_local_lines(text))
    return found


def offenders(root):
    return [entry for entry in scan(root) if entry not in ALLOW_LIST]


RAW_PREFIXES = ("", "u8", "u", "U", "L")
LINE_ENDS = ("\n", "\r\n")


def raw_string_splice(prefix, eol):
    # Sol r2 B1: translation reverts the splice inside the raw string, so the string ends at the second )x" and the
    # declaration is real; splicing first made an early terminator and the /* after it ate the declaration.
    return 'struct Foo { ~Foo() {} };%sauto s = %sR"x(foo)x\\%s"/*)x"; thread_local Foo g; // */%s' % (
        eol,
        prefix,
        eol,
        eol,
    )


# (file name, source) pairs that each hold a real or possible declaration; every one must be reported.
HIDDEN_SHAPES = (
    [("raw-%s-%s.cpp" % (prefix or "plain", "crlf" if eol == "\r\n" else "lf"), raw_string_splice(prefix, eol))
     for prefix in RAW_PREFIXES for eol in LINE_ENDS]
    + [
        ("splice-space.cpp", "thread_\\ \nlocal Foo g;\n"),
        ("splice-tab-crlf.cpp", "thread_\\\t\r\nlocal Foo g;\r\n"),
        ("splice-lone-cr.cpp", "thread_\\\rlocal Foo g;\r"),
        ("split-terminator-space.cpp", "/* c *\\ \n/ thread_local Foo g;\n"),
        ("glued-by-splice.cpp", "thread_local\\\nFoo g;\n"),
        ("digit-separator.cpp", "double d = 0x1.A'Bp0; const char c = '\"'; const char * s = \"//\"; thread_local Foo g;\n"),
        ("comment-markers.cpp", 'const char * s = "/*"; thread_local Foo g; const char * e = "*/";\n'),
        ("x.inc", "static thread_local std::vector<int> v;\n"),
        ("x.hxx", "static thread_local std::vector<int> v;\n"),
        ("x.hh", "static thread_local std::vector<int> v;\n"),
        ("x.ipp", "static thread_local std::vector<int> v;\n"),
        ("NOSUFFIX", "static thread_local std::vector<int> v;\n"),
    ]
)


class ThreadLocalGrammarTests(unittest.TestCase):
    def test_a_plain_declaration_is_found(self):
        self.assertEqual(["thread_local Foo g;"], thread_local_lines("thread_local Foo g;\n"))

    def test_a_static_local_is_found(self):
        self.assertEqual(["static thread_local Foo g;"], thread_local_lines("void f() {\n  static thread_local Foo g;\n}\n"))

    def test_a_macro_is_found(self):
        self.assertEqual(["#define X thread_local"], thread_local_lines("#define X thread_local\nX Foo g;\n"))

    def test_a_token_split_by_a_continuation_is_found(self):
        self.assertEqual(["thread_local Foo g;"], thread_local_lines("thread_\\\nlocal Foo g;\n"))

    def test_comments_are_found(self):
        # Flipped in r3: a token in a comment is reported on purpose and needs an allow-list row.
        self.assertEqual(
            ["// thread_local Foo g;", "/* thread_local", "thread_local */"],
            thread_local_lines("// thread_local Foo g;\n/* thread_local\n thread_local */\nint x;\n"),
        )

    def test_a_continued_line_comment_is_found(self):
        # Flipped in r3.
        self.assertEqual(
            ["thread_local Foo g;", "// comment thread_local Foo g;"],
            thread_local_lines("// comment \\\nthread_local Foo g;\nint x;\n"),
        )

    def test_comment_markers_inside_a_string_do_not_hide_the_token(self):
        self.assertEqual(
            ['const char * s = "//"; thread_local Foo g;'],
            thread_local_lines('const char * s = "//"; thread_local Foo g;\n'),
        )
        self.assertEqual(
            ['const char * s = "/*"; thread_local Foo g; const char * e = "*/";'],
            thread_local_lines('const char * s = "/*"; thread_local Foo g; const char * e = "*/";\n'),
        )

    def test_comment_markers_inside_a_raw_string_do_not_hide_the_token(self):
        self.assertEqual(
            ['const char * s = R"x(/*)x"; thread_local Foo g;'],
            thread_local_lines('const char * s = R"x(/*)x"; thread_local Foo g;\n'),
        )

    def test_comment_markers_inside_a_prefixed_raw_string_do_not_hide_the_token(self):
        # Adversary A2.
        for prefix in ("u8R", "uR", "UR", "LR"):
            source = 'const char * s = %s"x("/*)x";\nthread_local Foo g;\n' % prefix
            self.assertEqual(["thread_local Foo g;"], thread_local_lines(source), prefix)

    def test_a_comment_terminator_split_by_a_continuation_does_not_hide_the_token(self):
        # Adversary A3. Both views report it now: the physical line and the spliced one.
        self.assertEqual(
            ["/ thread_local Foo g;", "/* ignored */ thread_local Foo g;"],
            thread_local_lines("/* ignored *\\\n/ thread_local Foo g;\n"),
        )

    def test_a_splice_inside_a_raw_string_does_not_hide_the_token(self):
        # Sol r2 B1 (fable B1, opus B1): all five raw prefixes, LF and CRLF.
        for prefix in RAW_PREFIXES:
            for eol in LINE_ENDS:
                lines = thread_local_lines(raw_string_splice(prefix, eol))
                self.assertIn('"/*)x"; thread_local Foo g; // */', lines, (prefix, eol))

    def test_a_continuation_with_trailing_whitespace_is_a_continuation(self):
        # Opus r2 B2: GCC takes backslash, whitespace, newline as a continuation.
        for source in ("thread_\\ \nlocal Foo g;\n", "thread_\\\t\r\nlocal Foo g;\r\n", "thread_\\ \f\v\nlocal Foo g;\n"):
            self.assertEqual(["thread_local Foo g;"], thread_local_lines(source), repr(source))

    def test_a_lone_carriage_return_ends_a_continuation(self):
        self.assertEqual(["thread_local Foo g;"], thread_local_lines("thread_\\\rlocal Foo g;\r"))

    def test_a_comment_terminator_split_with_trailing_whitespace_does_not_hide_the_token(self):
        # Opus r2 B2, second input.
        self.assertIn("/ thread_local Foo g;", thread_local_lines("/* c *\\ \n/ thread_local Foo g;\n"))

    def test_the_unspliced_view_reports_a_token_a_continuation_would_glue(self):
        # Direction 1 of the union: the spliced view alone misses it.
        source = "thread_local\\\nFoo g;\n"
        self.assertIsNone(TOKEN.search(views(source)[1]))
        self.assertEqual(["thread_local\\"], thread_local_lines(source))

    def test_the_spliced_view_reports_a_token_split_by_a_continuation(self):
        # Direction 2 of the union: the unspliced view alone misses it.
        source = "thread_\\ \nlocal Foo g;\n"
        self.assertIsNone(TOKEN.search(views(source)[0]))
        self.assertEqual(["thread_local Foo g;"], thread_local_lines(source))

    def test_a_digit_separator_does_not_open_a_literal(self):
        self.assertEqual(
            ["int n = 1'000; thread_local Foo g;"],
            thread_local_lines("int n = 1'000; thread_local Foo g;\n"),
        )

    def test_a_prefixed_character_literal_does_not_desynchronise_quotes(self):
        # Adversary second pass A1.
        source = "const wchar_t c = L'\"'; const char * s = \"//\"; thread_local Foo g;\n"
        self.assertEqual([source.strip()], thread_local_lines(source))

    def test_a_hex_digit_separator_does_not_open_a_literal(self):
        self.assertEqual(
            ["int n = 0xA'B; thread_local Foo g;"],
            thread_local_lines("int n = 0xA'B; thread_local Foo g;\n"),
        )

    def test_a_hex_float_digit_separator_does_not_hide_the_token(self):
        # Adversary third pass A1 (r1).
        source = "double d = 0x1.A'Bp0; const char c = '\"'; const char * s = \"//\"; thread_local Foo g;\n"
        self.assertEqual([source.strip()], thread_local_lines(source))

    def test_a_pp_number_that_starts_at_its_point_still_has_a_digit_separator(self):
        for number in (".5'0", "return.5'0", "a.b.5'0"):
            source = "x = %s; const char c = '\"'; const char * s = \"//\"; thread_local Foo g;\n" % number
            self.assertEqual([source.strip()], thread_local_lines(source), number)

    def test_a_character_literal_after_a_point_is_still_a_literal(self):
        for literal in (".'\"'", "s.L'\"'"):
            source = "x = %s; const char * s = \"//\"; thread_local Foo g;\n" % literal
            self.assertEqual([source.strip()], thread_local_lines(source), literal)

    def test_a_longer_identifier_is_not_the_token(self):
        self.assertEqual([], thread_local_lines("int my_thread_local_count;\nint thread_locals;\n"))

    def test_every_hidden_shape_is_reported(self):
        for name, source in HIDDEN_SHAPES:
            self.assertTrue(thread_local_lines(source), name)

    def test_the_scan_reads_every_file_and_skips_other_trees(self):
        # Flipped in r3: there is no include list, so note.txt is reported too (opus r2 B3: .inc, .hxx, .hh, .ipp);
        # only the named binary suffixes are skipped.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "a").mkdir(parents=True)
            (root / "platform").mkdir()
            (root / "tests").mkdir()
            for name in ("x.cxx", "y.inl", "z.TCC", "p.inc", "q.hxx", "r.hh", "s.ipp", "NOSUFFIX"):
                (root / "src" / "a" / name).write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "w.h").write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "note.txt").write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "latin1.c").write_bytes(b"/* caf\xe9 */ thread_local int t;\n")
            (root / "platform" / "pixels.fpm").write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "icon.PNG").write_bytes(b"\x89PNG thread_local int t;\n")
            (root / "platform" / "ffmpeg.tar.xz").write_bytes(b"thread_local int t;\n")
            (root / "tests" / "v.cpp").write_text("thread_local int t;\n", encoding="utf-8")
            self.assertEqual(
                {
                    "src/a/x.cxx",
                    "src/a/y.inl",
                    "src/a/z.TCC",
                    "src/a/p.inc",
                    "src/a/q.hxx",
                    "src/a/r.hh",
                    "src/a/s.ipp",
                    "src/a/NOSUFFIX",
                    "platform/w.h",
                    "platform/note.txt",
                    "platform/latin1.c",
                    "platform/pixels.fpm",
                },
                {path for path, _ in scan(root)},
            )

    def test_the_product_tree_check_reports_each_hidden_shape(self):
        # Sol r2: all three product-tree tests passed with a hidden declaration in the tree.
        import tempfile

        for name, source in HIDDEN_SHAPES:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "src" / "processing").mkdir(parents=True)
                (root / "platform").mkdir()
                (root / "src" / "processing" / name).write_bytes(source.encode("utf-8"))
                self.assertTrue(offenders(root), name)


class ProductTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.found = scan(ROOT)

    def test_no_thread_local_outside_the_allow_list(self):
        self.assertEqual(
            [],
            [entry for entry in self.found if entry not in ALLOW_LIST],
            "thread_local in product code: use mlvThreadExitCache (src/processing/ThreadExitCache.h), "
            "or add an exact allow-list entry with the reason it registers no destructor "
            "(a comment or string that mentions it: reason '" + COMMENT_TEXT + "', or reword)",
        )

    def test_every_allow_list_entry_still_exists(self):
        found = set(self.found)
        self.assertEqual([], [entry for entry in ALLOW_LIST if entry not in found], "stale allow-list entry")

    def test_every_allow_list_entry_gives_a_reason(self):
        self.assertTrue(all(reason.strip() for reason in ALLOW_LIST.values()))


if __name__ == "__main__":
    unittest.main()
