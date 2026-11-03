package socket

import (
	"fmt"
	"net"
	"os/user"
	"strconv"

	"github.com/Hyodar/tundra/tools/pkg/attestation"
)

// callerOf names the peer with uid: uid 0 is the attested endpoint, any other uid its user
// name in /etc/passwd (a Service's unit stem unless it sets user=). A user named like the
// endpoint and a uid without a name are refused: neither may hold a tag of its own.
func callerOf(uid uint32, lookup func(string) (*user.User, error)) (string, error) {
	if uid == 0 {
		return attestation.EndpointCaller, nil
	}
	found, err := lookup(strconv.FormatUint(uint64(uid), 10))
	if err != nil || found.Username == "" {
		return "", fmt.Errorf("uid %d has no user in /etc/passwd, so its quotes would have no caller tag\n"+
			"fix: run the service as a user the image declares (a Service's own, or user=), not DynamicUser=", uid)
	}
	if found.Username == attestation.EndpointCaller {
		return "", fmt.Errorf("user %q (uid %d) is not the attested endpoint, which runs as root, and may not quote as it\n"+
			"fix: rename the service or give it another user=", found.Username, uid)
	}
	if len(found.Username) > attestation.MaxCaller {
		return "", fmt.Errorf("user name %q is longer than %d bytes\nfix: give the service a shorter user=",
			found.Username, attestation.MaxCaller)
	}
	return found.Username, nil
}

// identify names the peer of conn from its SO_PEERCRED credentials.
func (t *SocketTransport) identify(conn net.Conn) (string, error) {
	uid, err := t.peer(conn)
	if err != nil {
		return "", fmt.Errorf("cannot read the caller's credentials: %w\nfix: connect over the unix socket", err)
	}
	return callerOf(uid, t.lookup)
}

// tag is the user data the issuer binds for caller's request: its user data under caller's
// tag. A request that claims another caller than the peer is refused.
func tag(caller string, callerErr error, claimed string, userData []byte) ([]byte, error) {
	if callerErr != nil {
		return nil, callerErr
	}
	if claimed != "" && claimed != caller {
		return nil, fmt.Errorf("the caller is %q and may not quote as %q\nfix: only uid 0 quotes as %q; ask for a quote under your own user",
			caller, claimed, attestation.EndpointCaller)
	}
	return attestation.Tagged(caller, userData), nil
}
