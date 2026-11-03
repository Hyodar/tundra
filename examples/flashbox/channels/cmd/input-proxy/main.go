// input-proxy takes TCP connections and writes what they send into a Unix socket another
// process listens on. Nothing the socket's owner writes goes back to the sender, and a
// connection that cannot reach the socket is closed.
package main

import (
	"fmt"
	"io"
	"net"
	"os"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: input-proxy LISTEN-ADDRESS SOCKET")
		os.Exit(2)
	}
	listener, err := net.Listen("tcp", os.Args[1])
	if err != nil {
		fmt.Fprintln(os.Stderr, "input-proxy:", err)
		os.Exit(1)
	}
	for {
		conn, err := listener.Accept()
		if err != nil {
			fmt.Fprintln(os.Stderr, "input-proxy:", err)
			os.Exit(1)
		}
		go forward(conn, os.Args[2])
	}
}

func forward(conn net.Conn, socket string) {
	defer conn.Close()
	target, err := net.Dial("unix", socket)
	if err != nil {
		return
	}
	defer target.Close()
	go io.Copy(io.Discard, target)
	io.Copy(target, conn)
}
