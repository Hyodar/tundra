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
	Mode              string          `json:"mode"`
	Accepts           Accepts         `json:"accepts"`
}

// Server is one boot's endpoint: its TLS key exists only in memory.
type Server struct {
	cfg    *Config
	quoter Quoter
	log    *slog.Logger
	cert   tls.Certificate
	spki   []byte

	mu        sync.Mutex
	delivered map[string]bool
	unlocked  map[string]bool
	sshKey    string
}

type sessionKey struct{}

// session is one TLS connection: the nonces evidence was served for on it, each good for
// one write.
type session struct {
	mu     sync.Mutex
	nonces []string
}

func (s *session) remember(nonce string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.nonces = append(s.nonces, nonce)
	if len(s.nonces) > sessionNonces {
		s.nonces = s.nonces[len(s.nonces)-sessionNonces:]
	}
}

func (s *session) take(nonce string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, known := range s.nonces {
		if known == nonce {
			s.nonces = append(s.nonces[:i], s.nonces[i+1:]...)
			return true
		}
	}
	return false
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
		cfg:       cfg,
		quoter:    quoter,
		log:       log,
		cert:      tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key},
		spki:      spki,
		delivered: map[string]bool{},
		unlocked:  map[string]bool{},
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
	found := Accepts{Credentials: []string{}, Volumes: []string{}, SSHKey: s.cfg.SSH != nil}
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
	userData := Binding(nonce, s.spki, hostKeys, instanceKey)
	document, err := s.quoter.Issue(r.Context(), userData, nonce)
	if err != nil {
		s.log.Error("no quote", "error", err)
		fail(w, http.StatusServiceUnavailable, "no quote: %v", err)
		return
	}
	var metadata json.RawMessage
	if platformOf(document) == "simulator" {
		if metadata, err = s.quoter.Metadata(r.Context()); err != nil {
			fail(w, http.StatusServiceUnavailable, "no simulator metadata: %v", err)
			return
		}
	}
	mode := ""
	if s.cfg.ModeFile != "" {
		if data, err := os.ReadFile(s.cfg.ModeFile); err == nil {
			mode = strings.TrimSpace(string(data))
		}
	}
	if found := sessionOf(r); found != nil {
		found.remember(hex.EncodeToString(nonce))
	}
	if hostKeys == nil {
		hostKeys = []string{}
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
		Mode:              mode,
		Accepts:           s.accepts(),
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
// accepted only on the TLS session that fetched evidence for that nonce, once.
func (s *Server) bound(w http.ResponseWriter, r *http.Request, value any, nonce *string) bool {
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		fail(w, http.StatusBadRequest, "the body is unreadable or larger than %d bytes", maxBody)
		return false
	}
	if err := json.Unmarshal(body, value); err != nil {
		fail(w, http.StatusBadRequest, "the body is not the expected JSON: %v", err)
		return false
	}
	found := sessionOf(r)
	if found == nil || *nonce == "" || !found.take(strings.ToLower(*nonce)) {
		fail(w, http.StatusForbidden, "this session fetched no evidence for that nonce")
		return false
	}
	return true
}

type credentialsRequest struct {
	Nonce       string            `json:"nonce"`
	Credentials map[string]string `json:"credentials"`
}

func (s *Server) credentials(w http.ResponseWriter, r *http.Request) {
	var req credentialsRequest
	if !s.bound(w, r, &req, &req.Nonce) {
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
	Nonce      string `json:"nonce"`
	Volume     string `json:"volume"`
	Passphrase string `json:"passphrase"`
}

func (s *Server) unlock(w http.ResponseWriter, r *http.Request) {
	var req unlockRequest
	if !s.bound(w, r, &req, &req.Nonce) {
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
	Key   string `json:"key"`
}

func (s *Server) sshKeyHandler(w http.ResponseWriter, r *http.Request) {
	var req sshKeyRequest
	if !s.bound(w, r, &req, &req.Nonce) {
		return
	}
	if s.cfg.SSH == nil {
		fail(w, http.StatusNotFound, "the image takes no operator SSH key")
		return
	}
	fields := strings.Fields(req.Key)
	if len(fields) < 2 || !sshKeyPattern.MatchString(fields[0]+" "+fields[1]) {
		fail(w, http.StatusBadRequest, "not an OpenSSH public key line")
		return
	}
	key := fields[0] + " " + fields[1]
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
	if err := os.MkdirAll(s.cfg.SSH.Dir, 0o700); err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	line := []byte(keyRestrictions + " " + key + "\n")
	if err := writeAtomic(filepath.Join(s.cfg.SSH.Dir, authorizedKeys), line, 0o600, ""); err != nil {
		fail(w, http.StatusInternalServerError, "%v", err)
		return
	}
	if s.cfg.SSH.KeyPath != "" {
		if err := writeAtomic(s.cfg.SSH.KeyPath, []byte(fields[1]+"\n"), 0o600, ""); err != nil {
			fail(w, http.StatusInternalServerError, "%v", err)
			return
		}
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
