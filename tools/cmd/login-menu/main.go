// login-menu is a Login's shell and sshd's ForceCommand for it: it reads the login's menu,
// /etc/tundravm/logins/<user>.json, and the request the client sent ($SSH_ORIGINAL_COMMAND).
// An empty request prints the menu; any other names one entry and its typed arguments, and
// login-menu execs that entry's exact argv, through sudo -n for a root entry. Anything else
// is refused with a problem line and a fix line. Its own arguments (sshd passes -c and the
// forced command) are ignored: the request is never handed to a shell.
package main

import (
	"errors"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"syscall"

	"github.com/Hyodar/tundra/tools/pkg/menu"
)

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "login-menu: "+format+"\n", args...)
	os.Exit(2)
}

func main() {
	me, err := user.Current()
	if err != nil {
		fail("who am I: %v", err)
	}
	m, err := menu.Load(filepath.Join(menu.Directory, me.Username+".json"))
	if err != nil {
		fail("%v\nfix: the image declares no menu for %s; Login(%q, commands={...})", err, me.Username, me.Username)
	}
	argv, err := m.Parse(os.Getenv("SSH_ORIGINAL_COMMAND"))
	var problem *menu.Problem
	if errors.As(err, &problem) {
		fail("%s", problem.Error())
	}
	if argv == nil {
		fmt.Print(m.Listing())
		return
	}
	env := []string{
		"PATH=/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME=" + me.HomeDir,
		"USER=" + me.Username,
		"LOGNAME=" + me.Username,
		"LANG=C.UTF-8",
	}
	if err := syscall.Exec(argv[0], argv, env); err != nil {
		fail("%s: %v", argv[0], err)
	}
}
