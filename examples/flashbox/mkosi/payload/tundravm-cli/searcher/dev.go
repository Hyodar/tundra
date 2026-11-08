//go:build dev

package main

func init() {
	program.Commands = append(program.Commands, Command{
		Name: "shell", Help: "a shell as searcher",
		Run: func(v Values) []string { return Words("/usr/bin/bash") },
	})
}
