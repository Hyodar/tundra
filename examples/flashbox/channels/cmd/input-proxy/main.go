// input-proxy carries what the searcher sends over TLS into a Unix socket the container
// listens on, one way: nothing the socket's owner writes goes back to the sender. Each end
// holds an OpenSSH ed25519 key (-key) and pins the other's public key (-pin): the proxy
// answers with the container's host key, which attest pins, and lets in only the key in
// its pin file, read at each handshake. -send is the searcher's end, copying standard
// input to the proxy.
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/binary"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"math/big"
	"net"
	"os"
	"strings"
	"time"
)

const (
	name   = "input-proxy"
	pinned = "input-proxy pin"
)

const usage = `usage: input-proxy -listen ADDRESS -socket SOCKET -key KEY -pin FILE
       input-proxy -send ADDRESS -key KEY -pin FILE < INPUT
KEY is this end's OpenSSH ed25519 private key, FILE holds the other end's ssh-ed25519
public key (an authorized_keys, known_hosts or .pub line).`

func main() {
	listen := flag.String("listen", "", "the address the proxy listens on")
	socket := flag.String("socket", "", "the Unix socket the proxy writes into")
	send := flag.String("send", "", "the proxy's address, to send standard input to")
	keyPath := flag.String("key", "", "this end's OpenSSH ed25519 private key")
	pinPath := flag.String("pin", "", "the file holding the other end's public key")
	flag.Usage = func() { fmt.Fprintln(os.Stderr, usage) }
	flag.Parse()
	serving := *listen != "" && *socket != "" && *send == ""
	sending := *send != "" && *listen == "" && *socket == ""
	if flag.NArg() != 0 || *keyPath == "" || *pinPath == "" || serving == sending {
		flag.Usage()
		os.Exit(2)
	}
	key, err := readKey(*keyPath)
	if err == nil && sending {
		err = sendInput(*send, key, *pinPath, os.Stdin)
	} else if err == nil {
		var listener net.Listener
		if listener, err = net.Listen("tcp", *listen); err == nil {
			err = serve(listener, *socket, key, *pinPath)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "input-proxy:", err)
		os.Exit(1)
	}
}

func serve(listener net.Listener, socket string, key ed25519.PrivateKey, pinPath string) error {
	cert, err := certificate(key)
	if err != nil {
		return err
	}
	config := &tls.Config{
		MinVersion: tls.VersionTLS13,
		GetConfigForClient: func(*tls.ClientHelloInfo) (*tls.Config, error) {
			pool, err := trust(pinPath)
			if err != nil {
				return nil, err
			}
			return &tls.Config{
				MinVersion:   tls.VersionTLS13,
				Certificates: []tls.Certificate{cert},
				ClientAuth:   tls.RequireAndVerifyClientCert,
				ClientCAs:    pool,
			}, nil
		},
	}
	for {
		conn, err := listener.Accept()
		if err != nil {
			return err
		}
		go forward(tls.Server(conn, config), socket)
	}
}

func forward(conn *tls.Conn, socket string) {
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(10 * time.Second))
	if err := conn.Handshake(); err != nil {
		fmt.Fprintln(os.Stderr, "input-proxy: refused", conn.RemoteAddr(), err)
		return
	}
	conn.SetDeadline(time.Time{})
	target, err := net.Dial("unix", socket)
	if err != nil {
		fmt.Fprintln(os.Stderr, "input-proxy:", err)
		return
	}
	defer target.Close()
	go io.Copy(io.Discard, target)
	io.Copy(target, conn)
}

func sendInput(address string, key ed25519.PrivateKey, pinPath string, input io.Reader) error {
	cert, err := certificate(key)
	if err != nil {
		return err
	}
	pool, err := trust(pinPath)
	if err != nil {
		return err
	}
	conn, err := tls.Dial("tcp", address, &tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{cert},
		RootCAs:      pool,
		ServerName:   name,
	})
	if err != nil {
		return err
	}
	defer conn.Close()
	if _, err := io.Copy(conn, input); err != nil {
		return err
	}
	if err := conn.CloseWrite(); err != nil {
		return err
	}
	// The proxy refuses a key it does not pin after the client's handshake is done, so the
	// refusal arrives here, where the proxy's close would.
	_, err = io.Copy(io.Discard, conn)
	return err
}

func template(subject string, ca bool) *x509.Certificate {
	return &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: subject},
		NotBefore:             time.Unix(0, 0),
		NotAfter:              time.Date(9999, 12, 31, 0, 0, 0, 0, time.UTC),
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		IsCA:                  ca,
		BasicConstraintsValid: ca,
	}
}

func certificate(key ed25519.PrivateKey) (tls.Certificate, error) {
	leaf := template(name, false)
	leaf.DNSNames = []string{name}
	der, err := x509.CreateCertificate(rand.Reader, leaf, template(pinned, true), key.Public(), key)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, err
}

func trust(pinPath string) (*x509.CertPool, error) {
	pin, err := readPin(pinPath)
	if err != nil {
		return nil, err
	}
	// The pool's one root carries the pinned key, and each end's certificate names it as
	// its issuer, so crypto/tls verifies the peer's as it verifies any chain. A root's own
	// signature is never checked, so a throwaway key signs it.
	_, signer, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	root := template(pinned, true)
	der, err := x509.CreateCertificate(rand.Reader, root, root, pin, signer)
	if err != nil {
		return nil, err
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	pool.AddCert(parsed)
	return pool, nil
}

type wire struct {
	data []byte
	bad  bool
}

func (w *wire) take(size uint32) []byte {
	if w.bad || uint32(len(w.data)) < size {
		w.bad = true
		return nil
	}
	part := w.data[:size]
	w.data = w.data[size:]
	return part
}

func (w *wire) next() []byte {
	size := w.take(4)
	if size == nil {
		return nil
	}
	return w.take(binary.BigEndian.Uint32(size))
}

func readKey(path string) (ed25519.PrivateKey, error) {
	text, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(text)
	if block == nil || block.Type != "OPENSSH PRIVATE KEY" {
		return nil, fmt.Errorf("%s is not an OpenSSH private key: pass one ssh-keygen -t ed25519 wrote", path)
	}
	body, ok := bytes.CutPrefix(block.Bytes, []byte("openssh-key-v1\x00"))
	outer := &wire{data: body}
	cipher, kdf := string(outer.next()), string(outer.next())
	outer.next()
	outer.take(4)
	outer.next()
	inner := &wire{data: outer.next()}
	inner.take(8)
	kind, _, private := string(inner.next()), inner.next(), inner.next()
	switch {
	case !ok || outer.bad || inner.bad:
		return nil, fmt.Errorf("%s is not a readable OpenSSH private key", path)
	case cipher != "none" || kdf != "none":
		return nil, fmt.Errorf("%s has a passphrase: pass a copy without one (ssh-keygen -p -N '')", path)
	case kind != "ssh-ed25519" || len(private) != ed25519.PrivateKeySize:
		return nil, fmt.Errorf("%s holds a %s key: input-proxy takes ed25519 (ssh-keygen -t ed25519)", path, kind)
	}
	return ed25519.PrivateKey(private), nil
}

func readPin(path string) (ed25519.PublicKey, error) {
	text, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var keys []ed25519.PublicKey
	words := strings.Fields(string(text))
	for i := 0; i+1 < len(words); i++ {
		if words[i] != "ssh-ed25519" {
			continue
		}
		blob, err := base64.StdEncoding.DecodeString(words[i+1])
		if err != nil {
			return nil, fmt.Errorf("%s: the ssh-ed25519 key is not base64: %w", path, err)
		}
		key := &wire{data: blob}
		kind, public := string(key.next()), key.next()
		if key.bad || kind != "ssh-ed25519" || len(public) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("%s: the ssh-ed25519 key is not one", path)
		}
		keys = append(keys, ed25519.PublicKey(public))
	}
	switch len(keys) {
	case 1:
		return keys[0], nil
	case 0:
		return nil, errors.New(path + " holds no ssh-ed25519 public key: attest with an ed25519 identity, or pass its .pub line")
	}
	return nil, fmt.Errorf("%s holds %d ssh-ed25519 keys, so which to pin is unclear: pass the one line", path, len(keys))
}
