package endpoint

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/user"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	minNonce        = 16
	maxNonce        = 64
	maxBody         = 1 << 20
	sessionNonces   = 8
	authorizedKeys  = "authorized_keys"
	keyRestrictions = "no-port-forwarding,no-agent-forwarding,no-X11-forwarding"
)

var sshKeyPattern = regexp.MustCompile(`^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/]+={0,2}$`)

// Accepts is what the endpoint takes, as the evidence states it.
type Accepts struct {
	Credentials []string `json:"credentials"`
	Volumes     []string `json:"volumes"`
	SSHKey      bool     `json:"ssh_key"`
	// SSHLogin is the account the SSH key logs in as when it is not root.
	SSHLogin string `json:"ssh_login,omitempty"`
	// Owner is there in an image that pins its operator (an OperatorKey() volume): who
	// owns it, whose signature every write needs.
	Owner *OwnerState `json:"owner,omitempty"`
}

// ListenerKey is one attested TLS port's certificate key as the evidence states it: the
// base64 DER SubjectPublicKeyInfo the quote binds for that port.
type ListenerKey struct {
	Port      int    `json:"port"`
	PublicKey string `json:"public_key"`
}

// Evidence is the GET /evidence answer: the quote and every piece of material it binds.
type Evidence struct {
	Purpose           string          `json:"purpose"`
	Nonce             string          `json:"nonce"`
	UserData          string          `json:"user_data"`
	Document          string          `json:"document"`
	Metadata          json.RawMessage `json:"metadata"`
	TLSPublicKey      string          `json:"tls_public_key"`
	SSHHostKeys       []string        `json:"ssh_host_keys"`
	InstancePublicKey string          `json:"instance_public_key"`
	// Containers are each container's sshd host keys, which the quote binds by name.
	Containers  []ContainerKeys `json:"containers"`
	AttestedTLS []ListenerKey   `json:"attested_tls"`
	Mode        string          `json:"mode"`
	Accepts     Accepts         `json:"accepts"`
	// Firewall is the firewall's mode and the switches since boot that RTMR3 holds, in an
	// image with modes.
	Firewall *FirewallState `json:"firewall,omitempty"`
}

// Server is one boot's endpoint: its TLS key exists only in memory.
type Server struct {
	cfg    *Config
	quoter Quoter
	log    *slog.Logger
	cert   tls.Certificate
	spki   []byte

	mu        sync.Mutex
	ownerMu   sync.Mutex
	delivered map[string]bool
	unlocked  map[string]bool
	sshKey    string
	// headerWait bounds how long a rotation waits for disk-setup to write the new owner
	// into the headers: past its owner watcher's wait for a disk and one interval.
	headerWait time.Duration
}

type sessionKey struct{}

// session is one TLS connection: the nonces evidence was served for on it, each with the
// user data its quote bound and good for one write.
type session struct {
	mu     sync.Mutex
	served []served
}

type served struct {
	nonce    string
	userData []byte
}

func (s *session) remember(nonce string, userData []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.served = append(s.served, served{nonce, userData})
	if len(s.served) > sessionNonces {
		s.served = s.served[len(s.served)-sessionNonces:]
	}
}

// take spends nonce and returns the user data the evidence served for it bound.
func (s *session) take(nonce string) ([]byte, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, known := range s.served {
		if known.nonce == nonce {
			s.served = append(s.served[:i], s.served[i+1:]...)
			return known.userData, true
		}
	}
	return nil, false
}

// New makes the endpoint and its TLS key pair.
func New(cfg *Config, quoter Quoter, log *slog.Logger) (*Server, error) {
	if log == nil {
		log = slog.Default()
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generating the TLS key: %w", err)
	}
	spki, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		return nil, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 63))
	if err != nil {
		return nil, err
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: "tundravm attested endpoint"},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, fmt.Errorf("making the TLS certificate: %w", err)
	}
	return &Server{
		cfg:        cfg,
		quoter:     quoter,
		log:        log,
		cert:       tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key},
		spki:       spki,
		delivered:  map[string]bool{},
		unlocked:   map[string]bool{},
		headerWait: 60 * time.Second,
	}, nil
}

// PublicKey is the DER SubjectPublicKeyInfo of the TLS key the quotes bind.
func (s *Server) PublicKey() []byte { return s.spki }

// HTTPServer is the TLS server: every connection is its own session.
func (s *Server) HTTPServer() *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /evidence", s.evidence)
	mux.HandleFunc("POST /credentials", s.credentials)
	mux.HandleFunc("POST /unlock", s.unlock)
	mux.HandleFunc("POST /ssh-key", s.sshKeyHandler)
	mux.HandleFunc("POST /owner", s.rotate)
	mux.HandleFunc("POST /mode", s.switchMode)
	return &http.Server{
		Handler: mux,
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{s.cert},
			MinVersion:   tls.VersionTLS13,
		},
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return context.WithValue(ctx, sessionKey{}, &session{})
		},
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      90 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
}

// Serve serves TLS on l until it fails.
func (s *Server) Serve(l net.Listener) error {
	return s.HTTPServer().ServeTLS(l, "", "")
}

func fail(w http.ResponseWriter, status int, format string, args ...any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": fmt.Sprintf(format, args...)})
}

func answer(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}

func sessionOf(r *http.Request) *session {
	found, _ := r.Context().Value(sessionKey{}).(*session)
	return found
}

func (s *Server) accepts() Accepts {
	found := Accepts{
		Credentials: []string{}, Volumes: []string{}, SSHKey: s.takesSSHKey(), SSHLogin: s.sshLogin(), Owner: s.ownerState(),
	}
	seen := map[string]bool{}
	for _, c := range s.cfg.Credentials {
		if !seen[c.Name] {
			seen[c.Name] = true
			found.Credentials = append(found.Credentials, c.Name)
		}
	}
	for _, v := range s.cfg.Volumes {
		found.Volumes = append(found.Volumes, v.Name)
	}
	return found
}

// mode is what the execution gate recorded, "" when it is unknown.
func (s *Server) mode() string {
	if s.cfg.ModeFile == "" {
		return ""
	}
	data, err := os.ReadFile(s.cfg.ModeFile)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// takesSSHKey is whether POST /ssh-key can install a key, which logs in as root: in a
// simulated boot the first (or the owner's); on TDX only the owner's, once a volume's
// encrypted copy confirmed it this boot, because the host can always be first and can
// rewrite the plaintext header that names an owner.
func (s *Server) takesSSHKey() bool {
	return s.cfg.SSH != nil && s.takesLoginKey()
}

// sshLogin is the login POST /ssh-key writes the key for, "" for root or none.
func (s *Server) sshLogin() string {
	if s.cfg.SSH == nil {
		return ""
	}
	return s.cfg.SSH.Login
}

// takesLoginKey is whether a key that logs in may be installed: in a simulated boot, or
// from an owner a volume's encrypted copy confirmed this boot.
func (s *Server) takesLoginKey() bool {
	return s.mode() == "simulated" || s.confirmedOwner() != ""
}

// logsIn is whether a credential written to path is a key sshd logs in with.
func (s *Server) logsIn(path string) bool {
	dirs := []string{s.cfg.LoginKeys}
	if s.cfg.SSH != nil {
		dirs = append(dirs, s.cfg.SSH.Dir)
	}
	for _, c := range s.cfg.Containers {
		dirs = append(dirs, c.Keys)
	}
	for _, dir := range dirs {
		if dir != "" && strings.HasPrefix(filepath.Clean(path), filepath.Clean(dir)+"/") {
			return true
		}
	}
	return false
}

func (s *Server) evidence(w http.ResponseWriter, r *http.Request) {
	nonce, err := hex.DecodeString(r.URL.Query().Get("nonce"))
	if err != nil || len(nonce) < minNonce || len(nonce) > maxNonce {
		fail(w, http.StatusBadRequest, "nonce must be %d to %d bytes of hex", minNonce, maxNonce)
		return
	}
	hostKeys, err := ReadHostKeys(s.cfg.SSHHostKeys)
	if err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	instanceKey, err := ReadInstanceKey(s.cfg.InstanceKey)
	if err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	ingressKeys, err := ReadListenerKeys(s.cfg.AttestedTLS)
	if err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	containers, err := ReadContainerKeys(s.cfg.Containers)
	if err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	userData := Binding(nonce, s.spki, hostKeys, instanceKey, ContainerLines(containers), ingressKeys)
	document, metadata, firewall, err := s.quote(r.Context(), userData, nonce)
	if err != nil {
		s.log.Error("no quote", "error", err)
		fail(w, http.StatusServiceUnavailable, "%v", err)
		return
	}
	mode := s.mode()
	if found := sessionOf(r); found != nil {
		found.remember(hex.EncodeToString(nonce), userData)
	}
	if hostKeys == nil {
		hostKeys = []string{}
	}
	listeners := make([]ListenerKey, 0, len(ingressKeys))
	for i, key := range ingressKeys {
		listeners = append(listeners, ListenerKey{
			Port: s.cfg.AttestedTLS[i].Port, PublicKey: base64.StdEncoding.EncodeToString(key),
		})
	}
	answer(w, Evidence{
		Purpose:           Purpose,
		Nonce:             hex.EncodeToString(nonce),
		UserData:          hex.EncodeToString(userData),
		Document:          document,
		Metadata:          metadata,
		TLSPublicKey:      base64.StdEncoding.EncodeToString(s.spki),
		SSHHostKeys:       hostKeys,
		InstancePublicKey: hex.EncodeToString(instanceKey),
		Containers:        containers,
		AttestedTLS:       listeners,
		Mode:              mode,
		Accepts:           s.accepts(),
		Firewall:          firewall,
	})
}

func platformOf(document string) string {
	raw, err := hex.DecodeString(document)
	if err != nil {
		return ""
	}
	var doc struct {
		Platform string `json:"platform"`
	}
	if json.Unmarshal(raw, &doc) != nil {
		return ""
	}
	return doc.Platform
}

// bound reads a POST body into value and takes its nonce from the session: a write is
// accepted only on the TLS session that fetched evidence for that nonce, once. It returns
// the user data that evidence's quote bound, which an owner's signature covers.
func (s *Server) bound(w http.ResponseWriter, r *http.Request, value any, nonce *string) ([]byte, bool) {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		fail(w, http.StatusBadRequest, "the body is unreadable or larger than %d bytes", maxBody)
		return nil, false
	}
	if err := json.Unmarshal(body, value); err != nil {
		fail(w, http.StatusBadRequest, "the body is not the expected JSON: %v", err)
		return nil, false
	}
	found := sessionOf(r)
	if found == nil || *nonce == "" {
		fail(w, http.StatusForbidden, "this session fetched no evidence for that nonce")
		return nil, false
	}
	userData, ok := found.take(strings.ToLower(*nonce))
	if !ok {
		fail(w, http.StatusForbidden, "this session fetched no evidence for that nonce")
		return nil, false
	}
	return userData, true
}

type credentialsRequest struct {
	Nonce string `json:"nonce"`
	Signed
	Credentials map[string]string `json:"credentials"`
}

func (s *Server) credentials(w http.ResponseWriter, r *http.Request) {
	var req credentialsRequest
	userData, ok := s.bound(w, r, &req, &req.Nonce)
	if !ok {
		return
	}
	if _, ok := s.authorize(w, userData, req.Signed); !ok {
		return
	}
	if len(req.Credentials) == 0 {
		fail(w, http.StatusBadRequest, "no credentials")
		return
	}
	known := map[string][]Credential{}
	for _, c := range s.cfg.Credentials {
		known[c.Name] = append(known[c.Name], c)
	}
	values := map[string][]byte{}
	s.mu.Lock()
	defer s.mu.Unlock()
	for name, encoded := range req.Credentials {
		if _, ok := known[name]; !ok {
			fail(w, http.StatusNotFound, "the image declares no credential %s", name)
			return
		}
		if s.delivered[name] {
			fail(w, http.StatusConflict, "%s was delivered this boot already", name)
			return
		}
		for _, target := range known[name] {
			if s.logsIn(target.Path) && !s.takesLoginKey() {
				fail(w, http.StatusForbidden, "%s lands in %s, a login's keys: a TDX boot takes those only "+
					"from the owner an OperatorKey() volume's encrypted copy confirmed; unlock that volume, "+
					"then deliver again", name, target.Path)
				return
			}
		}
		value, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			fail(w, http.StatusBadRequest, "%s is not base64", name)
			return
		}
		values[name] = value
	}
	names := make([]string, 0, len(values))
	for name, value := range values {
		for _, target := range known[name] {
			mode, _ := parseMode(target.Mode)
			if err := writeAtomic(target.Path, value, mode, target.Owner); err != nil {
				s.log.Error("credential not written", "name", name, "error", err)
				fail(w, http.StatusInternalServerError, "%s not written: %v", name, err)
				return
			}
		}
		s.delivered[name] = true
		names = append(names, name)
		s.log.Info("credential delivered", "name", name)
	}
	answer(w, map[string][]string{"delivered": names})
}

type unlockRequest struct {
	Nonce string `json:"nonce"`
	Signed
	Volume     string `json:"volume"`
	Passphrase string `json:"passphrase"`
}

func (s *Server) unlock(w http.ResponseWriter, r *http.Request) {
	var req unlockRequest
	userData, ok := s.bound(w, r, &req, &req.Nonce)
	if !ok {
		return
	}
	if _, ok := s.authorize(w, userData, req.Signed); !ok {
		return
	}
	var pipe string
	for _, v := range s.cfg.Volumes {
		if v.Name == req.Volume {
			pipe = v.Pipe
		}
	}
	if pipe == "" {
		fail(w, http.StatusNotFound, "the image declares no operator-unlocked volume %s", req.Volume)
		return
	}
	passphrase, err := base64.StdEncoding.DecodeString(req.Passphrase)
	if err != nil || len(passphrase) == 0 {
		fail(w, http.StatusBadRequest, "the passphrase is empty or not base64")
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.unlocked[req.Volume] {
		fail(w, http.StatusConflict, "%s took its passphrase this boot already", req.Volume)
		return
	}
	if err := writePipe(pipe, passphrase); err != nil {
		fail(w, http.StatusConflict, "%s is not waiting for a passphrase: %v", req.Volume, err)
		return
	}
	s.unlocked[req.Volume] = true
	s.log.Info("passphrase handed to key-gen", "volume", req.Volume)
	answer(w, map[string]string{"unlocked": req.Volume})
}

type sshKeyRequest struct {
	Nonce string `json:"nonce"`
	Signed
	Key string `json:"key"`
}

func (s *Server) sshKeyHandler(w http.ResponseWriter, r *http.Request) {
	var req sshKeyRequest
	userData, ok := s.bound(w, r, &req, &req.Nonce)
	if !ok {
		return
	}
	if s.cfg.SSH == nil {
		fail(w, http.StatusNotFound, "the image takes no operator SSH key")
		return
	}
	if !s.takesSSHKey() {
		fail(w, http.StatusForbidden, "a TDX boot takes a delivered SSH key only from the owner an "+
			"OperatorKey() volume's encrypted copy confirmed: unlock that volume, then attest again "+
			"(an image without one bakes its keys: SshAccess(keys=))")
		return
	}
	fields := strings.Fields(req.Key)
	if len(fields) < 2 || !sshKeyPattern.MatchString(fields[0]+" "+fields[1]) {
		fail(w, http.StatusBadRequest, "not an OpenSSH public key line")
		return
	}
	key := fields[0] + " " + fields[1]
	signer, ok := s.authorize(w, userData, req.Signed)
	if !ok {
		return
	}
	if signer != "" && signer != key {
		fail(w, http.StatusForbidden, "the SSH key handed over is not the key that signed the delivery")
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sshKey == key {
		answer(w, map[string]string{"ssh_key": "kept"})
		return
	}
	if s.sshKey != "" {
		fail(w, http.StatusConflict, "another operator key was taken this boot")
		return
	}
	if err := s.writeSSHKey(key); err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	s.sshKey = key
	s.log.Info("operator SSH key taken")
	answer(w, map[string]string{"ssh_key": "taken"})
}

// writeAtomic writes data to a temporary name beside path, then renames it into place.
func writeAtomic(path string, data []byte, mode os.FileMode, owner string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o711); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if owner != "" {
		uid, gid, err := lookupOwner(owner)
		if err != nil {
			tmp.Close()
			return err
		}
		if err := tmp.Chown(uid, gid); err != nil {
			tmp.Close()
			return err
		}
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func lookupOwner(owner string) (int, int, error) {
	name, group, _ := strings.Cut(owner, ":")
	found, err := user.Lookup(name)
	if err != nil {
		return 0, 0, err
	}
	gid := found.Gid
	if group != "" {
		g, err := user.LookupGroup(group)
		if err != nil {
			return 0, 0, err
		}
		gid = g.Gid
	}
	uid, err := strconv.Atoi(found.Uid)
	if err != nil {
		return 0, 0, err
	}
	g, err := strconv.Atoi(gid)
	return uid, g, err
}

// writePipe hands data to the reader waiting on the FIFO at path; no reader is an error,
// never a wait.
func writePipe(path string, data []byte) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeNamedPipe == 0 {
		return fmt.Errorf("%s is not a FIFO", path)
	}
	f, err := os.OpenFile(path, os.O_WRONLY|syscall.O_NONBLOCK, 0)
	if errors.Is(err, syscall.ENXIO) {
		return fmt.Errorf("nothing reads %s", path)
	}
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}
