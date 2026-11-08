package main

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var tenant = Program{
	Login: "tenant",
	Commands: []Command{
		{Name: "greet", Help: "greet someone",
			Params: []Param{{Name: "who", Usage: "ada|bob",
				Check: func(value string) error { return OneOf(value, "ada", "bob") }}},
			Run: func(v Values) []string { return Words("/usr/bin/echo", "--name="+v.Get("who")) }},
		{Name: "debug", Sudo: true, Modes: []string{"maintenance", "sync"},
			Run: func(v Values) []string { return Words("/usr/bin/debug") }},
	},
}

const listing = `searcher: operate the flashbox searcher
ssh searcher@HOST COMMAND
  logs [--lines N] [--follow]  the searcher's delayed output
  reboot                       reboot the VM (as root)
` + shellRow + `  status                       the firewall mode in force (as root)
  toggle <mode>                switch the firewall mode (as root)
`

const logsHelp = `usage: ssh searcher@HOST logs [--lines N] [--follow]
the searcher's delayed output
  --lines N  lines from the end (1-10000, default 200)
  --follow   keep printing as the file grows
runs: /usr/bin/tail --lines <lines> [-f] /persistent/data/delayed/output.log
`

func mode(name string) func() (string, error) {
	return func() (string, error) { return name, nil }
}

func unread() (string, error) { return "", errors.New("permission denied") }

func TestRun(t *testing.T) {
	tail := "/usr/bin/tail --lines "
	output := " /persistent/data/delayed/output.log"
	logsFix := "fix: ssh searcher@HOST logs [--lines N] [--follow]\n"
	sudo := "/usr/bin/sudo -n -- "
	cases := []struct {
		program Program
		request string
		inForce func() (string, error)
		argv    string
		code    int
		stdout  string
		stderr  string
	}{
		{program, "", unread, "", 0, listing, ""},
		{program, " help ", unread, "", 0, listing, ""},
		{program, "--help", unread, "", 0, listing, ""},
		{program, "-h", unread, "", 0, listing, ""},
		{program, "help logs", unread, "", 2, "", "fix: ssh searcher@HOST logs --help"},
		{tenant, "", unread, "", 0, "  debug        (as root, maintenance or sync only)\n", ""},
		{program, "logs --help", unread, "", 0, logsHelp, ""},
		{program, "logs -h", unread, "", 0, logsHelp, ""},
		{program, "logs --lines 5 --help", unread, "", 0, logsHelp, ""},
		{program, "toggle --help", unread, "", 0, "  <mode>  (production|maintenance)\nruns: /usr/bin/mode-switch to <mode> (as root)\n", ""},
		{tenant, "greet --help", unread, "", 0, "runs: /usr/bin/echo --name=<who>\n", ""},
		{program, "logs", unread, tail + "200" + output, 0, "", ""},
		{program, "logs --lines 5 --follow", unread, tail + "5 -f" + output, 0, "", ""},
		{program, "logs --follow --lines=5", unread, tail + "5 -f" + output, 0, "", ""},
		{program, "toggle maintenance", unread, sudo + "/usr/bin/mode-switch to maintenance", 0, "", ""},
		{tenant, "greet ada", unread, "/usr/bin/echo --name=ada", 0, "", ""},
		{program, "nope", unread, "", 2, "", `searcher: searcher runs no "nope"` + "\nfix: ssh searcher@HOST with no command"},
		{program, "logs --lines 5 --lines=6", unread, "", 2, "", "logs: --lines is given twice\n" + logsFix},
		{program, "logs --follow=yes", unread, "", 2, "", "logs: --follow takes no value\n" + logsFix},
		{program, "logs --lines", unread, "", 2, "", "logs: --lines needs a value (1-10000)\n" + logsFix},
		{program, "logs --lines=", unread, "", 2, "", "logs: --lines needs a value (1-10000)\n" + logsFix},
		{program, "logs --lines 0", unread, "", 2, "", "logs: --lines is 0, outside 1-10000\n" + logsFix},
		{program, "logs --lines 03", unread, "", 2, "", `logs: --lines is "03", not a decimal number`},
		{program, "logs --tail 3", unread, "", 2, "", `logs takes no "--tail"` + "\n" + logsFix},
		{program, "logs 3", unread, "", 2, "", `logs takes no "3"` + "\n" + logsFix},
		{program, "logs -- --follow", unread, "", 2, "", `logs takes no "--"` + "\n" + logsFix},
		{program, "toggle", unread, "", 2, "", "toggle needs <mode> (production|maintenance)\nfix: ssh searcher@HOST toggle <mode>\n"},
		{program, "toggle tdx", unread, "", 2, "", `toggle: <mode> is "tdx", not one of production|maintenance`},
		{program, "toggle production;id", unread, "", 2, "", `not one of`},
		{tenant, "debug", mode("sync"), sudo + "/usr/bin/debug", 0, "", ""},
		{tenant, "debug", mode("production"), "", 1, "", "debug is offered in maintenance, sync only, and production is in force\nfix: switch to maintenance first"},
		{tenant, "debug", unread, "", 1, "", "the mode in force is unread: permission denied\nfix: mode-switch status"},
	}
	for _, c := range cases {
		var stdout, stderr bytes.Buffer
		argv, code := c.program.run(c.request, c.inForce, &stdout, &stderr)
		if strings.Join(argv, " ") != c.argv || code != c.code ||
			!strings.Contains(stdout.String(), c.stdout) || !strings.Contains(stderr.String(), c.stderr) ||
			(c.stdout == "") != (stdout.Len() == 0) || (c.stderr == "") != (stderr.Len() == 0) {
			t.Errorf("%s %q = %q, %d\nstdout:\n%s\nstderr:\n%s\nwant %q, %d, %q, %q",
				c.program.Login, c.request, argv, code, &stdout, &stderr, c.argv, c.code, c.stdout, c.stderr)
		}
	}
}

func TestShellIsCompiledOnlyIntoADevelopmentBuild(t *testing.T) {
	var stdout, stderr bytes.Buffer
	argv, _ := program.run("shell", unread, &stdout, &stderr)
	if built := argv != nil; built != (shellRow != "") {
		t.Errorf("shell runs %q in a build whose listing has %q", argv, shellRow)
	}
}

func TestModeIn(t *testing.T) {
	dir := t.TempDir()
	cases := []struct {
		log, mode string
		refused   bool
	}{
		{"", "production", false},
		{"tundravm:mode:maintenance:12\n", "maintenance", false},
		{"tundravm:mode:maintenance:12\ntundravm:mode:production:30\n", "production", false},
		{"tundravm:mode:maintenance:x\n", "", true},
		{"tundravm:mode:Maint:12\n", "", true},
	}
	for i, c := range cases {
		path := filepath.Join(dir, string(rune('a'+i)))
		if err := os.WriteFile(path, []byte(c.log), 0o600); err != nil {
			t.Fatal(err)
		}
		got, err := modeIn(path)
		if got != c.mode || (err != nil) != c.refused {
			t.Errorf("modeIn(%q) = %q, %v; want %q", c.log, got, err, c.mode)
		}
	}
	if got, err := modeIn(filepath.Join(dir, "missing")); got != "production" || err != nil {
		t.Errorf("modeIn(missing) = %q, %v; want production", got, err)
	}
}

func TestEnvironmentPassesTERMOnlyWhenItNamesATerminal(t *testing.T) {
	cases := map[string]string{"": "", "xterm-256color": "TERM=xterm-256color", "../x": "", "a b": "", "-x": ""}
	for term, want := range cases {
		env := environment("/home/t", "t", term)
		got := ""
		if last := env[len(env)-1]; strings.HasPrefix(last, "TERM=") {
			got = last
		}
		if got != want || env[1] != "HOME=/home/t" {
			t.Errorf("environment(TERM %q) = %v; want %q", term, env, want)
		}
	}
}
