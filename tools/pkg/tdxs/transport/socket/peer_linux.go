package socket

import (
	"errors"
	"net"
	"syscall"
)

// peerUID is the uid of the process at the other end of a unix socket, as the kernel
// recorded it at connect (SO_PEERCRED): the caller cannot choose it.
func peerUID(conn net.Conn) (uint32, error) {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, errors.New("not a unix socket connection")
	}
	raw, err := unixConn.SyscallConn()
	if err != nil {
		return 0, err
	}
	var cred *syscall.Ucred
	var inner error
	if err := raw.Control(func(fd uintptr) {
		cred, inner = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil {
		return 0, err
	}
	if inner != nil {
		return 0, inner
	}
	return cred.Uid, nil
}
