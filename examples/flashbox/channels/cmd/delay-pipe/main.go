// delay-pipe copies the lines a file gains to another file, each one a fixed delay late.
package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strconv"
	"time"
)

type entry struct {
	due  time.Time
	line string
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: delay-pipe SOURCE DESTINATION SECONDS")
		os.Exit(2)
	}
	seconds, err := strconv.Atoi(os.Args[3])
	if err != nil || seconds < 0 {
		fmt.Fprintln(os.Stderr, "delay-pipe: SECONDS is a whole number")
		os.Exit(2)
	}
	delay := time.Duration(seconds) * time.Second
	out, err := os.OpenFile(os.Args[2], os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		fmt.Fprintln(os.Stderr, "delay-pipe:", err)
		os.Exit(1)
	}
	defer out.Close()

	var queue []entry
	var offset int64
	var partial string
	for range time.Tick(time.Second) {
		queue, offset, partial = read(os.Args[1], delay, queue, offset, partial)
		for len(queue) > 0 && !queue[0].due.After(time.Now()) {
			fmt.Fprintln(out, queue[0].line)
			queue = queue[1:]
		}
	}
}

func read(path string, delay time.Duration, queue []entry, offset int64, partial string) ([]entry, int64, string) {
	file, err := os.Open(path)
	if err != nil {
		return queue, offset, partial
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return queue, offset, partial
	}
	if info.Size() < offset {
		offset, partial = 0, ""
	}
	if _, err := file.Seek(offset, io.SeekStart); err != nil {
		return queue, offset, partial
	}
	reader := bufio.NewReader(file)
	for {
		text, err := reader.ReadString('\n')
		offset += int64(len(text))
		if err != nil {
			return queue, offset, partial + text
		}
		queue = append(queue, entry{time.Now().Add(delay), partial + text[:len(text)-1]})
		partial = ""
	}
}
