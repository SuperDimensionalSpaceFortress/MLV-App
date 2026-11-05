"""No new thread_local in product code (CPU-SCOPE-LIVE-CRASH-FIX-1).

On mingw-w64 v11 the exe's TLS callback runs a thread_local's C++ destructor after libgcc's emutls key destructor has
freed its storage. Non-trivial per-thread caches go through mlvThreadExitCache (src/processing/ThreadExitCache.h).

Grammar: the token thread_local, after stripping comments, against an exact allow-list of (relative path, stripped
line, reason). A string literal is not stripped, so a token inside one still fails: conservative. It catches
`#define X thread_local` and a token split by a backslash-newline.

Limits:
- It cannot judge triviality on an allow-listed line: the reason is a human's claim.
- It does not see generated code or DLLs: Qt6Core.dll, libgomp-1.dll and libstdc++-6.dll reference
  __emutls_get_address and nothing here reaches them.
- __thread, __declspec(thread) and C11 _Thread_local are out of scope: they register no destructor.
- QThreadStorage is out of scope: a different mechanism, with no product use today.
- The object census (nm -u for __cxa_thread_atexit on every release .o) is the structural proof; this is only the
  source-level gate.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRECTORIES = ("platform", "src")
SUFFIXES = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".mm", ".inl", ".tcc"}
TOKEN = re.compile(r"\bthread_local\b")
SPLICE = re.compile(r"\\\r?\n")
RAW_STRING_OPEN = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\v\f\n]{0,16})\(')

ALLOW_LIST = {
    (
        "platform/qt/AudioPlayback.cpp",
        "static thread_local bool comInitialized = false;",
    ): "trivially destructible bool; registers no destructor",
    (
        "src/processing/ThreadExitCache.h",
        "T & mlvThreadExitCache() { static thread_local T t; return t; }",
    ): "the helper's non-MinGW branch; the failing ordering is specific to MinGW",
}


def is_digit_separator(text, i):
    """True when the ' at i sits inside a token that starts with a digit: a pp-number, not a literal's prefix."""
    j = i
    while j > 0 and (text[j - 1].isalnum() or text[j - 1] in "_'"):
        j -= 1
    return j < i and text[j].isdigit()


def strip_comments(text):
    """Blank // and /* */ comments, leaving string, character and raw string literals as they are."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        two = text[i : i + 2]
        if two == "//":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            stop = n if end < 0 else end + 2
            out.append(" " + "\n" * text.count("\n", i, stop))
            i = stop
            continue
        raw = RAW_STRING_OPEN.match(text, i) if c in "uULR" else None
        if raw and not (i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")):
            closing = ")" + raw.group(1) + '"'
            end = text.find(closing, raw.end())
            stop = n if end < 0 else end + len(closing)
            out.append(text[i:stop])
            i = stop
            continue
        if c in "\"'":
            # A digit separator (1'000, 0xA'B) is not a character literal; a prefixed literal (L'x', u8'x') is.
            if c == "'" and is_digit_separator(text, i):
                out.append(c)
                i += 1
                continue
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            stop = min(j + 1, n)
            out.append(text[i:stop])
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def thread_local_lines(text):
    """The stripped logical lines that hold the token. Splicing comes first, as it does in translation."""
    stripped = strip_comments(SPLICE.sub("", text))
    return [line.strip() for line in stripped.splitlines() if TOKEN.search(line)]


def scan(root):
    """Every (relative path, stripped line) in the scanned product trees."""
    found = []
    for directory in SCANNED_DIRECTORIES:
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix.lower() in SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                if TOKEN.search(SPLICE.sub("", text)):
                    relative = path.relative_to(root).as_posix()
                    found.extend((relative, line) for line in thread_local_lines(text))
    return found


class ThreadLocalGrammarTests(unittest.TestCase):
    def test_a_plain_declaration_is_found(self):
        self.assertEqual(["thread_local Foo g;"], thread_local_lines("thread_local Foo g;\n"))

    def test_a_static_local_is_found(self):
        self.assertEqual(["static thread_local Foo g;"], thread_local_lines("void f() {\n  static thread_local Foo g;\n}\n"))

    def test_a_macro_is_found(self):
        self.assertEqual(["#define X thread_local"], thread_local_lines("#define X thread_local\nX Foo g;\n"))

    def test_a_token_split_by_a_continuation_is_found(self):
        self.assertEqual(["thread_local Foo g;"], thread_local_lines("thread_\\\nlocal Foo g;\n"))

    def test_comments_are_not_found(self):
        self.assertEqual([], thread_local_lines("// thread_local Foo g;\n/* thread_local\n thread_local */\nint x;\n"))

    def test_a_continued_line_comment_is_not_found(self):
        self.assertEqual([], thread_local_lines("// comment \\\nthread_local Foo g;\nint x;\n"))

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
        # Adversary A3: splicing comes before comments in translation.
        self.assertEqual(
            ["thread_local Foo g;"],
            thread_local_lines("/* ignored *\\\n/ thread_local Foo g;\n"),
        )

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

    def test_a_longer_identifier_is_not_the_token(self):
        self.assertEqual([], thread_local_lines("int my_thread_local_count;\nint thread_locals;\n"))

    def test_the_scan_reads_every_suffix_and_skips_other_trees(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "a").mkdir(parents=True)
            (root / "platform").mkdir()
            (root / "tests").mkdir()
            for name in ("x.cxx", "y.inl", "z.TCC"):
                (root / "src" / "a" / name).write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "w.h").write_text("thread_local int t;\n", encoding="utf-8")
            (root / "platform" / "note.txt").write_text("thread_local int t;\n", encoding="utf-8")
            (root / "tests" / "v.cpp").write_text("thread_local int t;\n", encoding="utf-8")
            self.assertEqual(
                {"src/a/x.cxx", "src/a/y.inl", "src/a/z.TCC", "platform/w.h"},
                {path for path, _ in scan(root)},
            )


class ProductTreeTests(unittest.TestCase):
    def test_no_thread_local_outside_the_allow_list(self):
        offenders = [entry for entry in scan(ROOT) if entry not in ALLOW_LIST]
        self.assertEqual(
            [],
            offenders,
            "thread_local in product code: use mlvThreadExitCache (src/processing/ThreadExitCache.h), "
            "or add an exact allow-list entry with the reason it registers no destructor",
        )

    def test_every_allow_list_entry_still_exists(self):
        found = set(scan(ROOT))
        self.assertEqual([], [entry for entry in ALLOW_LIST if entry not in found], "stale allow-list entry")

    def test_every_allow_list_entry_gives_a_reason(self):
        self.assertTrue(all(reason.strip() for reason in ALLOW_LIST.values()))


if __name__ == "__main__":
    unittest.main()
