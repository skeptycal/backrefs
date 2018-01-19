r"""
Backrefs re.

Add the ability to use the following backrefs with re:

 - `\l`                                                       - Lowercase character class (search)
 - `\c`                                                       - Uppercase character class (search)
 - `\L`                                                       - Inverse of lowercase character class (search)
 - `\C`                                                       - Inverse of uppercase character class (search)
 - `\Q` and `\Q...\E`                                         - Escape/quote chars (search)
 - `\c` and `\C...\E`                                         - Uppercase char or chars (replace)
 - `\l` and `\L...\E`                                         - Lowercase char or chars (replace)
 - `[:ascii:]`                                                - Posix style classes (search)
 - `[:^ascii:]`                                               - Inverse Posix style classes (search)
 - `\p{Lu}` and \p{Letter} and `\p{gc=Uppercase_Letter}`      - Unicode properties (search Unicode)
 - `\p{block=Basic_Latin}` and `\p{InBasic_Latin}`            - Unicode block properties (search Unicode)
 - `\P{Lu}` and `\P{Letter}` and `\P{gc=Uppercase_Letter}`    - Inverse Unicode properties (search Unicode)
 - `\p{^Lu}` and `\p{^Letter}` and `\p{^gc=Uppercase_Letter}` - Inverse Unicode properties (search Unicode)
 - `\N{Black Club Suit}`                                      - Unicode character by name (search & replace)

Note
=========
 -  Various Unicode properties can be specified for `\p` or `\P`. They can also be placed in character groups,
    but you have to specify them separately.

    So the following is okay: `r"[\p{Lu}\p{Ll}]"` or `r"[\p{L}]"` etc.
    The following is *not* okay: `r"[\p{Lul}]"` or `r"[\p{Lu Ll}]"` etc.

 -  Unicode names can be specified in groups as well: `r"[\N{black club suit}]"`.

 -  Your search pattern must be a Unicode string in order to use Unicode property back references,
    but you do *not* have to use `re.UNICODE`.

 -  `\l`, `\L`, `\c`, and `\C` in searches will be ASCII ranges unless `re.UNICODE` is used.  This is to
    give some consistency with re's `\w`, `\W`, `\b`, `\B`, `\d`, `\D`, `\s` and `\S`. Some POSIX classes will
    also be affected.  See docs for more info.

Compiling
=========

~~~.py3
pattern = compile_search(r'somepattern', flags)
replace = compile_replace(pattern, r'\1 some replace pattern')
~~~

Usage
=========
Recommended to use compiling.  Assuming the above compiling:

~~~.py3
    text = pattern.sub(replace, 'sometext')
~~~

--or--

~~~.py3
    m = pattern.match('sometext')
    if m:
        text = replace(m)  # similar to m.expand(template)
~~~

Licensed under MIT
Copyright (c) 2011 - 2015 Isaac Muse <isaacmuse@gmail.com>
"""
from __future__ import unicode_literals
import sys
import sre_parse
import functools
import re
import unicodedata
from . import compat
from . import uniprops

_SCOPED_FLAG_SUPPORT = compat.PY36

MAXUNICODE = sys.maxunicode
NARROW = sys.maxunicode == 0xFFFF

# Expose some common re flags and methods to
# save having to import re and backrefs libs
DEBUG = re.DEBUG
I = re.I
IGNORECASE = re.IGNORECASE
L = re.L
LOCALE = re.LOCALE
M = re.M
MULTILINE = re.MULTILINE
S = re.S
DOTALL = re.DOTALL
U = re.U
UNICODE = re.UNICODE
X = re.X
VERBOSE = re.VERBOSE
if compat.PY3:
    A = re.A
    ASCII = re.ASCII
escape = re.escape
purge = re.purge
RE_TYPE = type(re.compile('', 0))

# Replace flags
FORMAT = 1

# Case upper or lower
_UPPER = 1
_LOWER = 2

_SEARCH_ASCII = re.ASCII if compat.PY3 else 0


class LoopException(Exception):
    """Loop exception."""


class GlobalRetryException(Exception):
    """Global retry exception."""


class ReplaceTokens(compat.Tokens):
    """Preprocess replace tokens."""

    _re_octal = re.compile(r'[0-7]{3}|0{1,2}', _SEARCH_ASCII)
    _re_group = re.compile(r'[1-9][0-9]?', _SEARCH_ASCII)
    _re_named_group = re.compile(r'g(?:<(?:[a-zA-Z]+[a-zA-Z\d_]*|0+|0*[1-9][0-9]?)>)?', _SEARCH_ASCII)
    _re_wide_unicode = re.compile(r'U(?:[0-9a-fA-F]{8})?', _SEARCH_ASCII)
    _re_narrow_unicode = re.compile(r'u(?:[0-9a-fA-F]{4})?', _SEARCH_ASCII)
    _re_named_unicode = re.compile(r'N(?:\{[\w ]+\})?', _SEARCH_ASCII)
    _re_byte = re.compile(r'x(?:[0-9a-fA-F]{2})?', _SEARCH_ASCII)
    _format_replace_group = re.compile(
        r'\{(?:[a-zA-Z]+[a-zA-Z\d_]*|0*(?:[1-9][0-9]?)?)?(?:\[[^\]]+\])?\}',
        _SEARCH_ASCII
    )

    def __init__(self, string, use_format=False, is_binary=False):
        """Initialize."""

        self.string = string
        self.binary = is_binary
        self.use_format = use_format
        self.max_index = len(string) - 1
        self.index = 0

    def get_format(self):
        """Get octal escape."""

        text = None
        m = self._format_replace_group.match(self.string, self.index - 2)
        if m:
            self.index = m.end(0)
            text = m.group(0)[1:-1]
        return text

    def get_octal(self):
        """Get octal escape."""

        text = None
        m = self._re_octal.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)
        return text

    def get_group(self):
        """Get group escape."""

        text = None
        m = self._re_group.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)
        return text

    def get_named_group(self):
        """Get group escape."""

        text = None
        m = self._re_named_group.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)
            if len(text) == 1:
                raise SyntaxError('Format for group is \\g<group_name_or_index>!')
            text = text
        return text

    def get_byte(self):
        """Get wide Unicode."""

        text = None
        m = self._re_byte.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)[1:]
            if not text:  # pragma: no cover
                raise SyntaxError('Format for byte is \\xXX!')
        return text

    def get_wide_unicode(self):
        """Get wide Unicode."""

        text = None
        m = self._re_wide_unicode.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)[1:]
            if not text:  # pragma: no cover
                raise SyntaxError('Format for wide Unicode is \\UXXXXXXXX!')
        return text

    def get_narrow_unicode(self):
        """Get wide Unicode."""

        text = None
        m = self._re_narrow_unicode.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)[1:]
            if not text:  # pragma: no cover
                raise SyntaxError('Format for Unicode is \\uXXXX!')
        return text

    def get_named_unicode(self):
        """Get named Unicode."""

        text = None
        m = self._re_named_unicode.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)
            if len(text) == 1:
                raise SyntaxError('Format for Unicode name is \\N{name}!')
            text = text[2:-1].strip()
        return text

    def __iter__(self):
        """Iterate."""

        return self

    def iternext(self):
        """
        Iterate through characters of the string.

        Count escaped l, L, c, C, E and backslash as a single char.
        """

        if self.index > self.max_index:
            raise StopIteration

        char = self.string[self.index]

        self.index += 1
        return char


class ReplaceTemplate(object):
    """Pre-replace template."""

    _ascii_letters = (
        'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
        'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
        'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
        'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z'
    )
    _standard_escapes = ('a', 'b', 'f', 'n', 'r', 't', 'v')
    _curly_brackets = ('{', '}')

    def __init__(self, pattern, template, use_format=False):
        """Initialize."""

        if isinstance(template, compat.binary_type):
            self.binary = True
        else:
            self.binary = False

        self._original = template
        self.use_format = use_format
        self.end_found = False
        self.group_slots = []
        self.literal_slots = []
        self.result = []
        self.span_stack = []
        self.single_stack = []
        self.slot = 0
        self.manual = False
        self.auto = False
        self.auto_index = 0
        self.pattern_hash = hash(pattern)

        self.parse_template(pattern)

    def handle_format(self, t, i):
        """Handle format."""

        if t == '{':
            t = next(i)
            if t == '{':
                self.get_single_stack()
                self.result.append(t)
            else:
                text = i.get_format()
                if text is None:
                    raise ValueError("Single unmatched curly bracket!")
                self.handle_format_group(text.strip())
        else:
            t = next(i)
            if t == '}':
                self.get_single_stack()
                self.result.append(t)
            else:
                raise ValueError("Single unmatched curly bracket!")

    def parse_octal(self, text):
        """Parse octal value."""

        value = int(text, 8)
        if value > 0xFF and self.binary:
            # Re fails on octal greater than 0o377 or 0xFF
            raise ValueError("octal escape value outside of range 0-0o377!")
        else:
            single = self.get_single_stack()
            if self.span_stack:
                text = self.convert_case(compat.uchr(value), self.span_stack[-1])
                value = ord(self.convert_case(text, single)) if single is not None else ord(text)
            elif single:
                value = ord(self.convert_case(compat.uchr(value), single))
            if value <= 0xFF:
                self.result.append('\\%03o' % value)
            else:
                self.result.append(compat.uchr(value))

    def parse_named_unicode(self, i):
        """Parse named Unicode."""

        value = ord(unicodedata.lookup(i.get_named_unicode()))
        single = self.get_single_stack()
        if self.span_stack:
            text = self.convert_case(compat.uchr(value), self.span_stack[-1])
            value = ord(self.convert_case(text, single)) if single is not None else ord(text)
        elif single:
            value = ord(self.convert_case(compat.uchr(value), single))
        if value <= 0xFF:
            self.result.append('\\%03o' % value)
        else:
            self.result.append(compat.uchr(value))

    def parse_unicode(self, i, wide=False):
        """Parse Unicode."""

        text = i.get_wide_unicode() if wide else i.get_narrow_unicode()
        value = int(text, 16)
        single = self.get_single_stack()
        if self.span_stack:
            text = self.convert_case(compat.uchr(value), self.span_stack[-1])
            value = ord(self.convert_case(text, single)) if single is not None else ord(text)
        elif single:
            value = ord(self.convert_case(compat.uchr(value), single))
        if value <= 0xFF:
            self.result.append('\\%03o' % value)
        else:
            self.result.append(compat.uchr(value))

    def parse_bytes(self, i):
        """Parse byte."""

        value = int(i.get_byte(), 16)
        single = self.get_single_stack()
        if self.span_stack:
            text = self.convert_case(chr(value), self.span_stack[-1])
            value = ord(self.convert_case(text, single)) if single is not None else ord(text)
        elif single:
            value = ord(self.convert_case(chr(value), single))
        self.result.append('\\%03o' % value)

    def reference(self, t, i):
        """Handle references."""
        octal = i.get_octal()
        if t.isdigit() and (self.use_format or octal):
            if not octal:
                octal = i.get_group()
            self.parse_octal(octal)
        elif (t.isdigit() or t == 'g') and not self.use_format:
            group = i.get_group()
            if not group:
                group = i.get_named_group()
            self.handle_group('\\' + group)
        elif t in self._standard_escapes:
            self.get_single_stack()
            self.result.append('\\' + t)
        elif t == "l":
            self.single_case(i, _LOWER)
        elif t == "L":
            self.span_case(i, _LOWER)
        elif t == "c":
            self.single_case(i, _UPPER)
        elif t == "C":
            self.span_case(i, _UPPER)
        elif t == "E":
            self.end_found = True
        elif not self.binary and not NARROW and t == "U":
            self.parse_unicode(i, True)
        elif not self.binary and t == "u":
            self.parse_unicode(i)
        elif not self.binary and t == "N":
            self.parse_named_unicode(i)
        elif t == "x":
            self.parse_bytes(i)
        elif self.use_format and t in self._curly_brackets:
            self.result.append('\\\\')
            self.handle_format(t, i)
        elif self.use_format and t == 'g':
            self.result.append('\\\\')
            self.result.append(t)
        else:
            value = '\\' + t
            self.get_single_stack()
            if self.span_stack:
                value = self.convert_case(value, self.span_stack[-1])
            self.result.append(value)

    def parse_template(self, pattern):
        """Parse template."""

        i = ReplaceTokens(
            (self._original.decode('latin-1') if self.binary else self._original),
            use_format=self.use_format,
            is_binary=self.binary
        )
        iter(i)
        self.result = [""]

        while True:
            try:
                t = next(i)
                if self.use_format and t in self._curly_brackets:
                    self.handle_format(t, i)
                elif t == '\\':
                    try:
                        t = next(i)
                        self.reference(t, i)
                    except StopIteration:
                        self.result.append(t)
                        raise
                else:
                    self.result.append(t)

            except StopIteration:
                break

        if len(self.result) > 1:
            self.literal_slots.append("".join(self.result))
            del self.result[:]
            self.result.append("")
            self.slot += 1

        if self.binary:
            self._template = "".join(self.literal_slots).encode('latin-1')
        else:
            self._template = "".join(self.literal_slots)
        self.groups, self.literals = sre_parse.parse_template(self._template, pattern)

    def span_case(self, i, case):
        """Uppercase or lowercase the next range of characters until end marker is found."""

        self.span_stack.append(case)
        self.end_found = False
        try:
            while not self.end_found:
                t = next(i)
                if self.use_format and t in self._curly_brackets:
                    self.handle_format(t, i)
                elif t == '\\':
                    try:
                        t = next(i)
                        self.reference(t, i)
                    except StopIteration:
                        self.result.append(t)
                        raise
                elif self.single_stack:
                    single = self.get_single_stack()
                    text = self.convert_case(t, case)
                    if single:
                        text = self.convert_case(text[0], single) + text[1:]
                    self.result.append(text)
                else:
                    self.result.append(self.convert_case(t, case))
                if self.end_found:
                    self.end_found = False
                    break
        except StopIteration:
            pass
        self.span_stack.pop()

    def convert_case(self, value, case):
        """Convert case."""

        if self.binary:
            cased = []
            for c in value:
                if c in self._ascii_letters:
                    cased.append(c.lower() if case == _LOWER else c.upper())
                else:
                    cased.append(c)
            return "".join(cased)
        else:
            return value.lower() if case == _LOWER else value.upper()

    def single_case(self, i, case):
        """Uppercase or lowercase the next character."""

        self.single_stack.append(case)
        try:
            t = next(i)
            if self.use_format and t in self._curly_brackets:
                self.handle_format(t, i)
            elif t == '\\':
                try:
                    t = next(i)
                    self.reference(t, i)
                except StopIteration:
                    self.result.append(t)
                    raise
            else:
                self.result.append(self.convert_case(t, self.get_single_stack()))
        except StopIteration:
            pass

    def get_single_stack(self):
        """Get the correct single stack item to use."""

        single = None
        while self.single_stack:
            single = self.single_stack.pop()
        return single

    def get_capture(self, text):
        """Get the capture."""

        capture = -1
        base = 10
        try:
            index = text.index("[")
            capture = text[index + 1:-1]
            text = text[:index]
            prefix = capture[1:3] if capture[0] == "-" else capture[:2]
            if prefix[0:1] == "0":
                char = prefix[-1]
                if char == "b":
                    base = 2
                elif char == "o":
                    base = 8
                elif char == "x":
                    base = 16
        except ValueError:
            pass

        if not isinstance(capture, int):
            try:
                capture = int(capture, base)
            except ValueError:
                raise ValueError("Capture index must be an integer!")
        return text, capture

    def handle_format_group(self, text):
        """Handle groups."""

        text, capture = self.get_capture(text)

        # Handle auto or manual format
        if text == "":
            if self.auto:
                text = compat.string_type(self.auto_index)
                self.auto_index += 1
            elif not self.manual and not self.auto:
                self.auto = True
                text = compat.string_type(self.auto_index)
                self.auto_index += 1
            else:
                raise ValueError("Cannot switch to auto format during manual format!")
        elif not self.manual and not self.auto:
            self.manual = True
        elif not self.manual:
            raise ValueError("Cannot switch to manual format during auto format!")

        self.handle_group(text, capture, True)

    def handle_group(self, text, capture=-1, is_format=False):
        """Handle groups."""

        if len(self.result) > 1:
            self.literal_slots.append("".join(self.result))
            if is_format:
                self.literal_slots.extend(["\\g<", text, ">"])
            else:
                self.literal_slots.append(text)
            del self.result[:]
            self.result.append("")
            self.slot += 1
        elif is_format:
            self.literal_slots.extend(["\\g<", text, ">"])
        else:
            self.literal_slots.append(text)

        self.group_slots.append(
            (
                self.slot,
                (
                    self.span_stack[-1] if self.span_stack else None,
                    self.get_single_stack(),
                    capture
                )
            )
        )
        self.slot += 1

    def get_base_template(self):
        """Return the unmodified template before expansion."""

        return self._original

    def get_group_index(self, index):
        """Find and return the appropriate group index."""

        g_index = None
        for group in self.groups:
            if group[0] == index:
                g_index = group[1]
                break
        return g_index

    def get_group_attributes(self, index):
        """Find and return the appropriate group case."""

        g_case = (None, None, -1)
        for group in self.group_slots:
            if group[0] == index:
                g_case = group[1]
                break
        return g_case


class ReplaceTemplateExpander(object):
    """Replacement template expander."""

    def __init__(self, match, template):
        """Initialize."""

        self.template = template
        self.index = -1
        self.end_found = False
        self.parent_span = []
        self.match = match

    def expand(self):
        """Using the template, expand the string."""

        sep = self.match.string[:0]
        text = []
        # Expand string
        for x in range(0, len(self.template.literals)):
            index = x
            l = self.template.literals[x]
            if l is None:
                g_index = self.template.get_group_index(index)
                span_case, single_case, capture = self.template.get_group_attributes(index)
                if capture not in (0, -1):
                    raise IndexError("'%d' is out of range!" % capture)
                l = self.match.group(g_index)
                if span_case is not None:
                    if span_case == _LOWER:
                        l = l.lower()
                    else:
                        l = l.upper()
                if single_case is not None:
                    if single_case == _LOWER:
                        l = l[0:1].lower() + l[1:]
                    else:
                        l = l[0:1].upper() + l[1:]
            text.append(l)

        return sep.join(text)


class SearchTokens(compat.Tokens):
    """Preprocess replace tokens."""

    _re_uniprops = re.compile(r'(?:p|P)(?:\{(?:\\.|[^\\}]+)+\}|[A-Z])?', _SEARCH_ASCII)
    _re_named_props = re.compile(r'N(?:\{[\w ]+\})?', _SEARCH_ASCII)
    _re_posix = re.compile(r'(?i)\[:(?:\\.|[^\\:}]+)+:\]', _SEARCH_ASCII)
    _re_flags = re.compile((r'\(\?([aiLmsux]+)\)' if compat.PY3 else r'\(\?([iLmsux]+)\)'), _SEARCH_ASCII)
    _re_comments = re.compile(r'\(\?\#(\\.|[^)])*\)', _SEARCH_ASCII)
    if compat.PY37:  # pragma: no cover
        _scoped_regex_flags = re.compile(r'\(\?(?:[aLu]|-?[imsx])+:', _SEARCH_ASCII)
    else:
        _scoped_regex_flags = re.compile(r'\(\?(?:-?[imsx])+:', _SEARCH_ASCII)

    def __init__(self, string, is_binary=False):
        """Initialize."""

        self.string = string
        self.binary = is_binary
        self.max_index = len(string) - 1
        self.index = 0

    def __iter__(self):
        """Iterate."""

        return self

    def rewind(self, index):
        """Rewind."""

        self.index = index

    def get_scoped_flags(self):  # pragma: no cover
        """Get scoped flags."""

        # Only PY36+ allow scoped flags
        if not _SCOPED_FLAG_SUPPORT:  # pragma: no cover
            return None

        text = None
        pattern = self._scoped_regex_flags
        m = pattern.match(self.string, self.index - 1)
        if m:
            text = m.group(0)
            self.index = m.end(0)
        return text

    def get_flags(self):
        """Get flags."""

        text = None
        m = self._re_flags.match(self.string, self.index - 1)
        if m:
            text = m.group(0)
            self.index = m.end(0)
        return text

    def get_comments(self):
        """Get comments."""

        text = None
        m = self._re_comments.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)
        return text

    def get_posix(self):
        """Get POSIX."""

        text = None
        m = self._re_posix.match(self.string, self.index - 1)
        if m:
            self.index = m.end(0)
            text = m.group(0)[2:-2]
        return text

    def get_named_property(self):
        """Get named property."""

        text = None
        m = self._re_named_props.match(self.string, self.index - 1)
        if m:
            text = m.group(0)
            if text == "N":
                raise SyntaxError('Format for Unicode name is \\N{name}!')
            self.index = m.end(0)
            text = text[1:]
        return text

    def get_unicode_property(self):
        """Get Unicode properties."""

        text = None
        m = self._re_uniprops.match(self.string, self.index - 1)
        if m:
            text = m.group(0)
            if text == 'p':
                raise SyntaxError('Format for Unicode property is \\p{property} or \\pP!')
            elif text == 'P':
                raise SyntaxError('Format for inverse Unicode property is \\P{property} or \\PP!')
            self.index = m.end(0)
            text = text[1:]
        return text

    def iternext(self):
        """
        Iterate through characters of the string.

        Count escaped l, L, c, C, E and backslash as a single char.
        """

        if self.index > self.max_index:
            raise StopIteration

        char = self.string[self.index]

        self.index += 1
        return char


class SearchTemplate(object):
    """Search Template."""

    _new_refs = ("e", "l", "L", "c", "C", "p", "P", "N", "Q", "E", "<", ">")
    _re_escape = r"\x1b"
    _re_start_wb = r"\b(?=\w)"
    _re_end_wb = r"\b(?<=\w)"
    _re_property_strip = re.compile(r'[\-_ ]', _SEARCH_ASCII)
    _re_property_gc = re.compile(
        r'''(?x)
        (?:((?:\\.|[^\\}]+)+?)[=:])?
        ((?:\\.|[^\\}]+)+)
        ''',
        _SEARCH_ASCII
    )

    def __init__(self, search, re_verbose=False, re_unicode=None):
        """Initialize."""

        if isinstance(search, compat.binary_type):
            self.binary = True
        else:
            self.binary = False

        self.search = search
        self.re_verbose = re_verbose
        self.re_unicode = re_unicode

    def process_quotes(self, string):
        """Process quotes."""

        escaped = False
        in_quotes = False
        current = []
        quoted = []
        i = SearchTokens(string, is_binary=self.binary)
        iter(i)
        for t in i:
            if not escaped and t == "\\":
                escaped = True
            elif escaped:
                escaped = False
                if t == "E":
                    if in_quotes:
                        current.append(escape("".join(quoted)))
                        quoted = []
                        in_quotes = False
                elif t == "Q" and not in_quotes:
                    in_quotes = True
                elif in_quotes:
                    quoted.extend(["\\", t])
                else:
                    current.extend(["\\", t])
            elif in_quotes:
                quoted.extend(t)
            else:
                current.append(t)

        if in_quotes and escaped:
            quoted.append("\\")
        elif escaped:
            current.append("\\")

        if quoted:
            current.append(escape("".join(quoted)))

        return "".join(current)

    def verbose_comment(self, t, i):
        """Handle verbose comments."""

        current = []
        escaped = False

        try:
            while t != "\n":
                if not escaped and t == "\\":
                    escaped = True
                    current.append(t)
                elif escaped:
                    escaped = False
                    if t in self._new_refs:
                        current.append("\\")
                    current.append(t)
                else:
                    current.append(t)
                t = next(i)
        except StopIteration:
            pass

        if t == "\n":
            current.append(t)
        return current

    def flags(self, text, scoped=False):
        """Analyze flags."""

        global_retry = False
        if compat.PY3 and ('a' in text or 'L' in text) and self.unicode:
            self.unicode = False
            if not _SCOPED_FLAG_SUPPORT or not scoped:
                self.temp_global_flag_swap["unicode"] = True
                global_retry = True
        elif 'u' in text and not self.unicode and not self.binary:
            self.unicode = True
            if not _SCOPED_FLAG_SUPPORT or not scoped:
                self.temp_global_flag_swap["unicode"] = True
                global_retry = True
        if _SCOPED_FLAG_SUPPORT and '-x' in text and self.verbose:
            self.verbose = False
        elif 'x' in text and not self.verbose:
            self.verbose = True
            if not _SCOPED_FLAG_SUPPORT or not scoped:
                self.temp_global_flag_swap["verbose"] = True
                global_retry = True
        if global_retry:
            raise GlobalRetryException('Global Retry')

    def reference(self, t, i, in_group=False):
        """Handle references."""

        current = []

        if not in_group and t == "<":
            current.append(self._re_start_wb)
        elif not in_group and t == ">":
            current.append(self._re_end_wb)
        elif t == "e":
            current.append(self._re_escape)
        elif t == "l":
            current.extend(self.letter_case_props(_LOWER, in_group))
        elif t == "L":
            current.extend(self.letter_case_props(_LOWER, in_group, negate=True))
        elif t == "c":
            current.extend(self.letter_case_props(_UPPER, in_group))
        elif t == "C":
            current.extend(self.letter_case_props(_UPPER, in_group, negate=True))

        elif t == 'p':
            text = i.get_unicode_property()
            if text.startswith("{"):
                text = text[1:-1]
            current.extend(self.unicode_props(text, in_group))
            if in_group:
                self.found_property = True
        elif t == 'P':
            text = i.get_unicode_property()
            if text.startswith("{"):
                text = text[1:-1]
            current.extend(self.unicode_props(text, in_group, negate=True))
            if in_group:
                self.found_property = True
        elif t == "N":
            text = i.get_named_property()[1:-1]
            current.extend(self.unicode_name(text, in_group))
            if in_group:
                self.found_property = True
        else:
            current.extend(["\\", t])
        return current

    def subgroup(self, t, i):
        """Handle parenthesis."""

        current = []

        # (?flags)
        flags = i.get_flags()
        if flags:
            self.flags(flags[2:-1])
            return [flags]

        # (?#comment)
        comments = i.get_comments()
        if comments:
            return [comments]

        verbose = self.verbose
        unicode_flag = self.unicode

        # (?flags:pattern)
        flags = i.get_scoped_flags()
        if flags:  # pragma: no cover
            t = flags
            self.flags(flags[2:-1], scoped=True)

        current = []
        try:
            while t != ')':
                if not current:
                    current.append(t)
                else:
                    current.extend(self.normal(t, i))

                t = next(i)
        except StopIteration:
            pass

        # Restore flags after group
        self.verbose = verbose
        self.unicode = unicode_flag

        if t == ")":
            current.append(t)
        return current

    def char_groups(self, t, i):
        """Handle character groups."""

        current = []
        pos = i.index - 1
        found = False
        escaped = False
        first = None
        self.found_property = False

        try:
            while True:
                if not escaped and t == "\\":
                    escaped = True
                elif escaped:
                    escaped = False
                    current.extend(self.reference(t, i, True))
                elif t == "[" and not found:
                    found = True
                    first = pos
                    current.append(t)
                elif t == "[":
                    posix = i.get_posix()
                    if posix:
                        current.extend(self.posix_props(posix, in_group=True))
                        self.found_property = True
                        pos = i.index - 2
                    else:
                        current.append(t)
                elif t == "^" and found and (pos == first + 1):
                    first = pos
                    current.append(t)
                elif t == "]" and found and (pos != first + 1):
                    found = False
                    current.append(t)
                    break
                else:
                    current.append(t)
                pos += 1
                t = next(i)
        except StopIteration:
            pass

        if escaped:
            current.append(t)

        # Handle properties that return an empty string.
        # This will occur when a property's values exceed
        # either the Unicode char limit on a narrow system,
        # or the ASCII limit in a byte string pattern.
        if self.found_property:
            value = "".join(current)
            if value == '[]':
                # We specified some properities, but they are all
                # out of reach.  Therefore we can match nothing.
                current = ['[^%s]' % ('\x00-\xff' if self.binary else uniprops.UNICODE_RANGE)]
            elif value == '[^]':
                current = ['[%s]' % ('\x00-\xff' if self.binary else uniprops.UNICODE_RANGE)]
            else:
                current = [value]

        return current

    def normal(self, t, i):
        """Handle normal chars."""

        current = []

        if t == "\\":
            try:
                t = next(i)
                current.extend(self.reference(t, i))
            except StopIteration:
                current.append(t)
        elif t == "(":
            current.extend(self.subgroup(t, i))
        elif self.verbose and t == "#":
            current.extend(self.verbose_comment(t, i))
        elif t == "[":
            current.extend(self.char_groups(t, i))
        else:
            current.append(t)
        return current

    def posix_props(self, prop, in_group=False):
        """
        Insert POSIX properties.

        Posix style properties are not as forgiving
        as Unicode properties.  Case does matter,
        and whitespace and '-' and '_' will not be tolerated.
        """

        try:
            if self.binary or not self.unicode:
                pattern = uniprops.get_posix_property(prop, (uniprops.POSIX_BINARY if self.binary else uniprops.POSIX))
            else:
                pattern = uniprops.get_posix_property(prop, uniprops.POSIX_UNICODE)
        except Exception:
            raise ValueError('Invalid POSIX property!')
        if not in_group and not pattern:  # pragma: no cover
            pattern = '^%s' % ('\x00-\xff' if self.binary else uniprops.UNICODE_RANGE)

        return [pattern]

    def unicode_name(self, name, in_group=False):
        """Insert Unicode value by its name."""

        value = ord(unicodedata.lookup(name))
        if (self.binary and value > 0xFF) or (not self.binary and NARROW and value > MAXUNICODE):
            value = ""
        if not in_group and value == "":
            return '[^%s]' % ('\x00-\xff' if self.binary else uniprops.UNICODE_RANGE)
        elif value == "":
            return value
        else:
            return ['\\%03o' % value if value <= 0xFF else compat.uchr(value)]

    def unicode_props(self, props, in_group, negate=False):
        """
        Insert Unicode properties.

        Unicode properties are very forgiving.
        Case doesn't matter and `[ -_]` will be stripped out.
        """

        # 'GC = Some_Unpredictable-Category Name' -> 'gc=someunpredictablecategoryname'
        props = self._re_property_strip.sub("", props.lower())
        category = None

        # \p{^negated} Strip off the caret after evaluation.
        if props.startswith("^"):
            negate = not negate
        if props.startswith("^"):
            props = props[1:]

        # Get the property and value.
        # If a property is present and not block,
        # we can assume GC as that is all we support.
        # If we are wrong it will fail.
        m = self._re_property_gc.match(props)
        props = m.group(2)
        if m.group(1):
            if uniprops.is_enum(m.group(1)):
                category = m.group(1)
            elif props in ('y', 'yes', 't', 'true'):
                category = 'binary'
            elif props in ('n', 'no', 'f', 'false'):
                category = 'binary'
                negate = not negate
            else:
                raise ValueError('Invalid Unicode property!')

        v = uniprops.get_unicode_property(("^" if negate else "") + props, category, self.binary)
        if not in_group:
            if not v:
                v = '^%s' % ('\x00-\xff' if self.binary else uniprops.UNICODE_RANGE)
            v = "[%s]" % v
        properties = [v]

        return properties

    def letter_case_props(self, case, in_group, negate=False):
        """Insert letter (ASCII or Unicode) case properties."""

        # Use traditional ASCII upper/lower case unless:
        #    1. The strings fed in are not binary
        #    2. And the the unicode flag was used
        if not in_group:
            v = self.posix_props(("^" if negate else "") + ("upper" if case == _UPPER else "lower"), in_group=in_group)
            v[0] = "[%s]" % v[0]
        else:
            v = self.posix_props(("^" if negate else "") + ("upper" if case == _UPPER else "lower"), in_group=in_group)
        return v

    def main_group(self, i):
        """The main group: group 0."""

        current = []
        while True:
            try:
                t = next(i)
                current.extend(self.normal(t, i))
            except StopIteration:
                break
        return current

    def apply(self):
        """Apply search template."""

        self.verbose = bool(self.re_verbose)
        self.unicode = bool(self.re_unicode)
        self.global_flag_swap = {
            "unicode": ((self.re_unicode is not None) if not compat.PY37 else False),
            "verbose": False
        }
        self.temp_global_flag_swap = {
            "unicode": False,
            "verbose": False
        }
        if compat.PY3:
            self.ascii = self.re_unicode is not None and not self.re_unicode
        else:
            self.ascii = False
        if compat.PY3 and not self.unicode and not self.ascii:
            self.unicode = True

        new_pattern = []
        string = self.process_quotes(self.search.decode('latin-1') if self.binary else self.search)

        i = SearchTokens(string, is_binary=self.binary)
        iter(i)

        retry = True
        while retry:
            retry = False
            try:
                new_pattern = self.main_group(i)
            except GlobalRetryException:
                # Prevent a loop of retry over and over for a pattern like ((?u)(?a))
                # or (?-x:(?x))
                if self.temp_global_flag_swap['unicode']:
                    if self.global_flag_swap['unicode']:
                        raise LoopException('Global unicode flag recursion.')
                    else:
                        self.global_flag_swap["unicode"] = True
                if self.temp_global_flag_swap['verbose']:
                    if self.global_flag_swap['verbose']:
                        raise LoopException('Global verbose flag recursion.')
                    else:
                        self.global_flag_swap['verbose'] = True
                self.temp_global_flag_swap = {
                    "unicode": False,
                    "verbose": False
                }
                i.rewind(0)
                retry = True

        return "".join(new_pattern).encode('latin-1') if self.binary else "".join(new_pattern)


class Replace(compat.Immutable):
    """Bre compiled replace object."""

    __slots__ = ("func", "use_format", "pattern_hash")

    def __init__(self, func, use_format, pattern_hash):
        """Initialize."""

        super(Replace, self).__init__(func=func, use_format=use_format, pattern_hash=pattern_hash)

    def __call__(self, *args, **kwargs):
        """Call."""

        return self.func(*args, **kwargs)


class Bre(compat.Immutable):
    """Bre object."""

    __slots__ = ("pattern", "auto_compile")

    def __init__(self, pattern, auto_compile=True):
        """Initialization."""

        super(Bre, self).__init__(pattern=pattern, auto_compile=auto_compile)

    def _auto_compile(self, template, use_format=False):
        """Compile repalcement."""

        is_replace = _is_replace(template)
        is_string = isinstance(template, (compat.string_type, compat.binary_type))
        if is_replace and use_format != template.use_format:
            raise ValueError("Compiled replace cannot be a format object!")
        if is_replace or (is_string and self.auto_compile):
            return self.compile(template, (FORMAT if use_format and not is_replace else 0))
        elif is_string and use_format:
            # Reject an attempt to run format replace when auto-compiling
            # of template strings has been disabled and we are using a
            # template string.
            raise AttributeError('Format replaces cannot be called without compiling replace template!')
        else:
            return template

    def compile(self, repl, flags=0):
        """Compile replace."""

        return compile_replace(self.pattern, repl, flags)

    def search(self, string, pos=0, endpos=sys.maxsize):
        """Apply `search`."""

        return self.pattern.search(string, pos, endpos)

    def match(self, string, pos=0, endpos=sys.maxsize):
        """Apply `match`."""

        return self.pattern.match(string, pos, endpos)

    if compat.PY34:
        def fullmatch(self, string, pos=0, endpos=sys.maxsize):
            """Apply `fullmatch`."""

            return self.pattern.fullmatch(string, pos, endpos)

    def split(self, string, maxsplit=0):
        """Apply `split`."""

        return self.pattern.split(string, maxsplit)

    def findall(self, string, pos=0, endpos=sys.maxsize):
        """Apply `findall`."""

        return self.pattern.findall(string, pos, endpos)

    def finditer(self, string, pos=0, endpos=sys.maxsize):
        """Apply `finditer`."""

        return self.pattern.finditer(string, pos, endpos)

    def sub(self, repl, string, count=0):
        """Apply `sub`."""

        return self.pattern.sub(self._auto_compile(repl), string, count)

    def subf(self, repl, string, count=0):  # noqa B002
        """Apply `sub` with format style replace."""

        return self.pattern.sub(self._auto_compile(repl, True), string, count)

    def subn(self, repl, string, count=0):
        """Apply `subn` with format style replace."""

        return self.pattern.subn(self._auto_compile(repl), string, count)

    def subfn(self, repl, string, count=0):  # noqa B002
        """Apply `subn` after applying backrefs."""

        return self.pattern.subn(self._auto_compile(repl, True), string, count)


def _is_replace(obj):
    """Check if object is a replace object."""

    return isinstance(obj, (ReplaceTemplate, Replace))


def _apply_replace_backrefs(m, repl=None, flags=0):
    """Expand with either the `ReplaceTemplate` or compile on the fly, or return None."""

    if m is None:
        raise ValueError("Match is None!")
    else:
        if isinstance(repl, Replace):
            return repl(m)
        elif isinstance(repl, ReplaceTemplate):
            return ReplaceTemplateExpander(m, repl).expand()
        elif isinstance(repl, (compat.string_type, compat.binary_type)):
            return ReplaceTemplateExpander(m, ReplaceTemplate(m.re, repl, bool(flags & FORMAT))).expand()


def _apply_search_backrefs(pattern, flags=0):
    """Apply the search backrefs to the search pattern."""

    if isinstance(pattern, (compat.string_type, compat.binary_type)):
        re_verbose = bool(VERBOSE & flags)
        re_unicode = None
        if compat.PY3 and bool((ASCII | LOCALE) & flags):
            re_unicode = False
        elif bool(UNICODE & flags):
            re_unicode = True
        pattern = SearchTemplate(pattern, re_verbose, re_unicode).apply()
    elif isinstance(pattern, RE_TYPE):
        if flags:
            raise ValueError("Cannot process flags argument with a compiled pattern!")
    else:
        raise TypeError("Not a string or compiled pattern!")
    return pattern


def compile(pattern, flags=0, auto_compile=True):
    """Compile both the search or search and replace into one object."""

    return Bre(compile_search(pattern, flags), auto_compile)


def compile_search(pattern, flags=0):
    """Compile with extended search references."""

    return re.compile(_apply_search_backrefs(pattern, flags), flags)


def compile_replace(pattern, repl, flags=0):
    """Construct a method that can be used as a replace method for `sub`, `subn`, etc."""

    call = None
    if pattern is not None and isinstance(pattern, RE_TYPE):
        if isinstance(repl, (compat.string_type, compat.binary_type)):
            repl = ReplaceTemplate(pattern, repl, bool(flags & FORMAT))
            call = Replace(
                functools.partial(_apply_replace_backrefs, repl=repl), repl.use_format, repl.pattern_hash
            )
        elif isinstance(repl, Replace):
            if flags:
                raise ValueError("Cannot process flags argument with a compiled pattern!")
            if repl.pattern_hash != hash(pattern):
                raise ValueError("Pattern hash doesn't match hash in compiled replace!")
            call = repl
        elif isinstance(repl, ReplaceTemplate):
            if flags:
                raise ValueError("Cannot process flags argument with a ReplaceTemplate!")
            call = Replace(
                functools.partial(_apply_replace_backrefs, repl=repl), repl.use_format, repl.pattern_hash
            )
        else:
            raise TypeError("Not a valid type!")
    else:
        raise TypeError("Pattern must be a compiled regular expression!")
    return call


def _assert_expandable(repl, use_format=False):
    """Check if replace template is expandable."""

    if isinstance(repl, (Replace, ReplaceTemplate)):
        if repl.use_format != use_format:
            if use_format:
                raise ValueError("Replace not compiled as a format replace")
            else:
                raise ValueError("Replace should not be compiled as a format replace!")
    elif not isinstance(repl, (compat.string_type, compat.binary_type)):
        raise TypeError("Expected string, buffer, or compiled replace!")


# Convenience methods like re has, but slower due to overhead on each call.
# It is recommended to use compile_search and compile_replace
def expand(m, repl):
    """Expand the string using the replace pattern or function."""

    _assert_expandable(repl)
    return _apply_replace_backrefs(m, repl)


def expandf(m, format):  # noqa B002
    """Expand the string using the format replace pattern or function."""

    _assert_expandable(format, True)
    return _apply_replace_backrefs(m, format, flags=FORMAT)


def search(pattern, string, flags=0):
    """Apply `search` after applying backrefs."""

    return re.search(_apply_search_backrefs(pattern, flags), string, flags)


def match(pattern, string, flags=0):
    """Apply `match` after applying backrefs."""

    return re.match(_apply_search_backrefs(pattern, flags), string, flags)


if compat.PY34:
    def fullmatch(pattern, string, flags=0):
        """Apply `fullmatch` after applying backrefs."""

        return re.fullmatch(_apply_search_backrefs(pattern, flags), string, flags)


def split(pattern, string, maxsplit=0, flags=0):
    """Apply `split` after applying backrefs."""

    return re.split(_apply_search_backrefs(pattern, flags), string, maxsplit, flags)


def findall(pattern, string, flags=0):
    """Apply `findall` after applying backrefs."""

    return re.findall(_apply_search_backrefs(pattern, flags), string, flags)


def finditer(pattern, string, flags=0):
    """Apply `finditer` after applying backrefs."""

    return re.finditer(_apply_search_backrefs(pattern, flags), string, flags)


def sub(pattern, repl, string, count=0, flags=0):
    """Apply `sub` after applying backrefs."""

    is_replace = _is_replace(repl)
    is_string = isinstance(repl, (compat.string_type, compat.binary_type))
    if is_replace and repl.use_format:
        raise ValueError("Compiled replace cannot be a format object!")

    pattern = compile_search(pattern, flags)
    return re.sub(
        pattern, (compile_replace(pattern, repl) if is_replace or is_string else repl), string, count, flags
    )


def subf(pattern, format, string, count=0, flags=0):  # noqa B002
    """Apply `sub` with format style replace."""

    is_replace = _is_replace(format)
    is_string = isinstance(format, (compat.string_type, compat.binary_type))
    if is_replace and not format.use_format:
        raise ValueError("Compiled replace is not a format object!")

    pattern = compile_search(pattern, flags)
    rflags = FORMAT if is_string else 0
    return re.sub(
        pattern, (compile_replace(pattern, format, flags=rflags) if is_replace or is_string else format),
        string, count, flags
    )


def subn(pattern, repl, string, count=0, flags=0):
    """Apply `subn` with format style replace."""

    is_replace = _is_replace(repl)
    is_string = isinstance(repl, (compat.string_type, compat.binary_type))
    if is_replace and repl.use_format:
        raise ValueError("Compiled replace cannot be a format object!")

    pattern = compile_search(pattern, flags)
    return re.subn(
        pattern, (compile_replace(pattern, repl) if is_replace or is_string else repl), string, count, flags
    )

def subfn(pattern, format, string, count=0, flags=0):  # noqa B002
    """Apply `subn` after applying backrefs."""

    is_replace = _is_replace(format)
    is_string = isinstance(format, (compat.string_type, compat.binary_type))
    if is_replace and not format.use_format:
        raise ValueError("Compiled replace is not a format object!")

    pattern = compile_search(pattern, flags)
    rflags = FORMAT if is_string else 0
    return re.subn(
        pattern, (compile_replace(pattern, format, flags=rflags) if is_replace or is_string else format),
        string, count, flags
    )
