package menu

import (
	"errors"
	"strings"
	"testing"
)

func word(s string) Word { return Word{Word: &s} }

var fixture = Menu{
	Login: "tenant",
	Entries: []Entry{
		{Name: "greeting", Argv: []Word{word("/usr/bin/cat"), word("/etc/greeting")}},
		{Name: "logs", Root: true, Argv: []Word{
			word("/usr/bin/journalctl"), word("-n"), {Arg: "lines", Int: []int64{1, 1000}},
		}},
		{Name: "switch", Root: true, Argv: []Word{
			word("/usr/bin/mode-switch"), word("to"), {Arg: "mode", Choice: []string{"production", "maintenance"}},
		}},
	},
}

func TestParse(t *testing.T) {
	sudo := strings.Join(Sudo, " ")
	cases := []struct {
		request string
		argv    string
		refused string
	}{
		{"", "", ""},
		{"   ", "", ""},
		{"greeting", "/usr/bin/cat /etc/greeting", ""},
		{"logs 3", sudo + " /usr/bin/journalctl -n 3", ""},
		{"  logs   1000 ", sudo + " /usr/bin/journalctl -n 1000", ""},
		{"switch production", sudo + " /usr/bin/mode-switch to production", ""},
		{"sh", "", `tenant runs no "sh"`},
		{"/bin/sh -c id", "", `tenant runs no "/bin/sh"`},
		{"logs", "", "logs needs <lines> (1-1000)"},
		{"logs 0", "", "is 0, outside 1-1000"},
		{"logs 1001", "", "is 1001, outside 1-1000"},
		{"logs 03", "", `is "03", not a decimal number`},
		{"logs -3", "", `is "-3", not a decimal number`},
		{"logs 3;id", "", `is "3;id", not a decimal number`},
		{"logs 99999999999", "", "not a decimal number"},
		{"logs 3 4", "", `logs takes no "4"`},
		{"greeting --help", "", `greeting takes no "--help"`},
		{"switch tdx", "", `is "tdx", not one of production|maintenance`},
		{"switch Production", "", "not one of"},
	}
	for _, c := range cases {
		argv, err := fixture.Parse(c.request)
		if c.refused != "" {
			var problem *Problem
			if !errors.As(err, &problem) || !strings.Contains(problem.What, c.refused) || problem.Fix == "" {
				t.Errorf("Parse(%q) = %v, %v; want refused with %q", c.request, argv, err, c.refused)
			}
			continue
		}
		if err != nil || strings.Join(argv, " ") != c.argv {
			t.Errorf("Parse(%q) = %q, %v; want %q", c.request, argv, err, c.argv)
		}
	}
}

func TestListingNamesEveryEntry(t *testing.T) {
	got := fixture.Listing()
	for _, want := range []string{
		"tenant runs (ssh tenant@HOST COMMAND):\n",
		"  greeting\n",
		"  logs <lines>   lines 1-1000, as root\n",
		"  switch <mode>  mode production|maintenance, as root\n",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("Listing() lacks %q:\n%s", want, got)
		}
	}
}

func TestCheckRefusesAMenuTheImageCouldNotWrite(t *testing.T) {
	cases := map[string]Menu{
		"no entries":     {Login: "t"},
		"relative path":  {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("cat")}}}},
		"argument first": {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{{Arg: "x", Int: []int64{0, 1}}}}}},
		"untyped":        {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("/x"), {Arg: "x"}}}}},
		"bounds":         {Login: "t", Entries: []Entry{{Name: "a", Argv: []Word{word("/x"), {Arg: "x", Int: []int64{5, 1}}}}}},
		"twice": {Login: "t", Entries: []Entry{
			{Name: "a", Argv: []Word{word("/x")}}, {Name: "a", Argv: []Word{word("/y")}},
		}},
	}
	for name, m := range cases {
		if err := m.Check(); err == nil {
			t.Errorf("%s: Check() accepted %+v", name, m)
		}
	}
	if err := fixture.Check(); err != nil {
		t.Fatal(err)
	}
}
