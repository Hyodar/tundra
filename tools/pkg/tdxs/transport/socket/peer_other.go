//go:build !linux

package socket

import (
	"errors"
	"net"
)

// peerUID needs SO_PEERCRED, which only Linux has: tdxs runs in the guest.
func peerUID(net.Conn) (uint32, error) {
	return 0, errors.New("SO_PEERCRED is Linux only")
}
