package endpoint

import (
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/Hyodar/tundra/tools/pkg/owner"
)

// Signed is what every write carries in an image that pins its operator (a volume with an
// owner file): the signer's OpenSSH public key and its SSH signature (namespace
// tundravm-endpoint, ssh-keygen -Y sign) over the user data of the evidence the write's
// nonce fetched, in lower-case hex. The user data binds that nonce and this boot's TLS key,
// so a signature another endpoint saw (the client picks the nonce) authorizes nothing here.
type Signed struct {
	Signer    string `json:"signer"`
	Signature string `json:"signature"`
}

// OwnerState is the evidence's word on who operates the instance; the fingerprint is ""
// while no signed delivery has pinned anyone.
type OwnerState struct {
	Fingerprint string `json:"fingerprint"`
	// Mismatch is set when disk-setup found a volume's header naming another owner than
	// the copy inside the volume (owner.MismatchMessage), which keeps it closed.
	Mismatch string `json:"mismatch,omitempty"`
}

type ownerRequest struct {
	Nonce string `json:"nonce"`
	Signed
	Key          string `json:"key"`
	KeySignature string `json:"key_signature"`
}

// ownerFiles are the boot's owner files, one per volume that pins its operator.
func (s *Server) ownerFiles() []string {
	var paths []string
	for _, v := range s.cfg.Volumes {
		if v.Owner != "" {
			paths = append(paths, v.Owner)
		}
	}
	return paths
}

// currentOwner is the key the owner files name, "" for none; volumes owned by two keys are
// an error.
func (s *Server) currentOwner() (string, error) {
	found := ""
	for _, path := range s.ownerFiles() {
		key, err := owner.ReadFile(path)
		if err != nil {
			return "", err
		}
		if key != "" && found != "" && key != found {
			return "", fmt.Errorf("the volumes are owned by two keys, %s and %s", owner.Fingerprint(found), owner.Fingerprint(key))
		}
		if key != "" {
			found = key
		}
	}
	return found, nil
}

// confirmedOwner is the current owner when a volume's encrypted copy confirmed it this
// boot, "" otherwise.
func (s *Server) confirmedOwner() string {
	current, err := s.currentOwner()
	if err != nil || current == "" {
		return ""
	}
	for _, path := range s.ownerFiles() {
		if confirmed, err := owner.ReadFile(path + owner.ConfirmedSuffix); err == nil && confirmed == current {
			return current
		}
	}
	return ""
}

func (s *Server) ownerState() *OwnerState {
	if len(s.ownerFiles()) == 0 {
		return nil
	}
	state := &OwnerState{}
	for _, path := range s.ownerFiles() {
		if said, err := os.ReadFile(path + owner.MismatchSuffix); err == nil {
			state.Mismatch = strings.TrimSpace(string(said))
		}
	}
	if key, err := s.currentOwner(); err == nil {
		state.Fingerprint = owner.Fingerprint(key)
	}
	return state
}

// signedOver is the message an owner signs for a write: the hex user data its nonce bound.
func signedOver(userData []byte) []byte { return []byte(hex.EncodeToString(userData)) }

// authorize lets a bound write through: anyone's in an image that pins no operator; in one
// that does, only a write signed over its evidence's user data by the owner, or by anyone
// while no one is pinned, which makes the signer the owner of every volume not pinned yet.
// It answers the refusal itself.
func (s *Server) authorize(w http.ResponseWriter, userData []byte, req Signed) (string, bool) {
	paths := s.ownerFiles()
	if len(paths) == 0 {
		return "", true
	}
	s.ownerMu.Lock()
	defer s.ownerMu.Unlock()
	current, err := s.currentOwner()
	if err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return "", false
	}
	if req.Signer == "" || req.Signature == "" {
		fail(w, http.StatusForbidden, "unsigned: this instance takes deliveries signed by its operator's SSH key only")
		return "", false
	}
	signer, _, err := owner.Normalize(req.Signer)
	if err != nil {
		fail(w, http.StatusBadRequest, "signer: %v", err)
		return "", false
	}
	if err := owner.Verify(signer, signedOver(userData), req.Signature); err != nil {
		fail(w, http.StatusForbidden, "the signature over the evidence's user data: %v", err)
		return "", false
	}
	if current != "" && signer != current {
		fail(w, http.StatusForbidden, "the volumes are owned by %s, not %s", owner.Fingerprint(current), owner.Fingerprint(signer))
		return "", false
	}
	for _, path := range paths {
		if pinned, err := owner.ReadFile(path); err != nil || pinned != "" {
			continue
		}
		if err := owner.WriteFile(path, signer); err != nil {
			fail(w, http.StatusInternalServerError, "the owner is not pinned: %v", err)
			return "", false
		}
		s.log.Info("owner pinned", "owner", owner.Fingerprint(signer), "file", path)
	}
	return signer, true
}

// rotate is POST /owner: the owner's signed write naming a new key, with that key's own
// signature over the same nonce, replaces the owner in every owner file (the old one kept
// beside it until disk-setup has written the new one into the header) and the SSH key.
func (s *Server) rotate(w http.ResponseWriter, r *http.Request) {
	var req ownerRequest
	userData, ok := s.bound(w, r, &req, &req.Nonce)
	if !ok {
		return
	}
	if len(s.ownerFiles()) == 0 {
		fail(w, http.StatusNotFound, "the image pins no operator: it has no OperatorKey() volume")
		return
	}
	if current, err := s.currentOwner(); err == nil && current == "" {
		fail(w, http.StatusConflict, "no one owns the volumes yet: the first signed delivery pins its key")
		return
	}
	old, ok := s.authorize(w, userData, req.Signed)
	if !ok {
		return
	}
	key, _, err := owner.Normalize(req.Key)
	if err != nil {
		fail(w, http.StatusBadRequest, "key: %v", err)
		return
	}
	if err := owner.Verify(key, signedOver(userData), req.KeySignature); err != nil {
		fail(w, http.StatusForbidden, "the new key's signature over the evidence's user data: %v", err)
		return
	}
	s.ownerMu.Lock()
	defer s.ownerMu.Unlock()
	for _, path := range s.ownerFiles() {
		if err := owner.WriteFile(path+owner.FromSuffix, old); err != nil {
			fail(w, http.StatusInternalServerError, "%v", err)
			return
		}
		if err := owner.WriteFile(path, key); err != nil {
			fail(w, http.StatusInternalServerError, "%v", err)
			return
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sshKey != "" && s.cfg.SSH != nil {
		if err := s.writeSSHKey(key); err != nil {
			fail(w, http.StatusInternalServerError, "%v", err)
			return
		}
		s.sshKey = key
	}
	s.log.Info("owner rotated", "from", owner.Fingerprint(old), "to", owner.Fingerprint(key))
	answer(w, map[string]string{"owner": owner.Fingerprint(key)})
}

// writeSSHKey makes key the one authorized_keys line: root's (and KeyPath's blob), or the
// login's, restricted, which sshd reads as that login (0644).
func (s *Server) writeSSHKey(key string) error {
	if s.cfg.SSH.Login != "" {
		path := filepath.Join(s.cfg.LoginKeys, s.cfg.SSH.Login)
		return writeAtomic(path, []byte("restrict "+key+"\n"), 0o644, "")
	}
	if err := os.MkdirAll(s.cfg.SSH.Dir, 0o700); err != nil {
		return err
	}
	fields := strings.Fields(key)
	line := []byte(keyRestrictions + " " + key + "\n")
	if err := writeAtomic(filepath.Join(s.cfg.SSH.Dir, authorizedKeys), line, 0o600, ""); err != nil {
		return err
	}
	if s.cfg.SSH.KeyPath != "" {
		return writeAtomic(s.cfg.SSH.KeyPath, []byte(fields[1]+"\n"), 0o600, "")
	}
	return nil
}
