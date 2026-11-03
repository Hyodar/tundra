package attestation

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
)

// EndpointCaller is the caller tag tdxs gives uid 0: the attested endpoint on 8745. Every
// other caller is tagged with its user name, so no other service's quote passes for the
// endpoint's.
const EndpointCaller = "endpoint"

// MaxCaller bounds a caller tag: a user name is at most 32 bytes.
const MaxCaller = 64

// Tagged is the user data tdxs hands its issuer for caller: lp(caller) || userData, where
// lp(x) is x's length as a big-endian uint32 followed by x. The quote's report data is
// SHA-256(Tagged(caller, userData) || nonce), and the document's user_data is these bytes.
func Tagged(caller string, userData []byte) []byte {
	out := make([]byte, 4, 4+len(caller)+len(userData))
	binary.BigEndian.PutUint32(out, uint32(len(caller)))
	out = append(out, caller...)
	return append(out, userData...)
}

var errUntagged = errors.New("the quote's user data carries no caller tag: the image's tdxs predates caller tags")

// Untag splits a document's tagged user data into its caller and the caller's user data.
// Untagged user data (from a tdxs that tags nothing) is an error.
func Untag(tagged []byte) (string, []byte, error) {
	if len(tagged) < 4 {
		return "", nil, errUntagged
	}
	size := binary.BigEndian.Uint32(tagged[:4])
	if size == 0 || size > MaxCaller || int(size) > len(tagged)-4 {
		return "", nil, errUntagged
	}
	return string(tagged[4 : 4+size]), tagged[4+size:], nil
}

// RequireCaller checks that tagged is caller's tag over userData: the quote was issued to
// caller, for that user data.
func RequireCaller(tagged []byte, caller string, userData []byte) error {
	found, inner, err := Untag(tagged)
	if err != nil {
		return fmt.Errorf("refused: %w\nfix: rebuild the image with this tundravm, whose tdxs tags each quote with its caller", err)
	}
	if found != caller {
		return fmt.Errorf("refused: the quote was issued to %q, not %q: another service in the guest asked for it\n"+
			"fix: check the quote against the service that asked for it, or attest again", found, caller)
	}
	if !bytes.Equal(inner, userData) {
		return fmt.Errorf("refused: the quote's tagged user data is not %q's binding\nfix: attest again", caller)
	}
	return nil
}
