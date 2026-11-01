// The signing proxy examples/attested-node/image.py builds from this directory
// (Local("signer")). It answers JSON-RPC on -listen by forwarding each request to the
// Nethermind endpoint -upstream. For eth_blockNumber, eth_getBlockByNumber and
// eth_getBlockByHash it signs keccak256 of the response's canonical result (compact JSON,
// object keys sorted) with this boot's instance key: a recoverable 65-byte signature
// r || s || v (v is 27 or 28, as ecrecover takes it), sent hex-encoded in
// X-Tundra-Signature with the signer's compressed public key in X-Tundra-Signer. Every
// other request passes through unsigned. GET /signer answers the public key.
package main

import (
	"bytes"
	"encoding/asn1"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
	"golang.org/x/crypto/sha3"
)

const (
	signatureHeader = "X-Tundra-Signature"
	signerHeader    = "X-Tundra-Signer"
	maxRequest      = 1 << 20
	maxResponse     = 64 << 20
)

var signed = map[string]bool{
	"eth_blockNumber":      true,
	"eth_getBlockByNumber": true,
	"eth_getBlockByHash":   true,
}

var secp256k1OID = asn1.ObjectIdentifier{1, 3, 132, 0, 10}

type ecPrivateKey struct {
	Version    int
	PrivateKey []byte
	Curve      asn1.ObjectIdentifier `asn1:"optional,explicit,tag:0"`
	PublicKey  asn1.BitString        `asn1:"optional,explicit,tag:1"`
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8547", "the loopback address to answer on")
	upstream := flag.String("upstream", "http://127.0.0.1:8545/", "Nethermind's JSON-RPC URL")
	keyFile := flag.String("key", "", "the instance key (default $CREDENTIALS_DIRECTORY/instance-key)")
	flag.Parse()
	key, err := loadKey(*keyFile)
	if err != nil {
		log.Fatal(err)
	}
	p := newProxy(*upstream, key)
	log.Printf("signing as %s, forwarding to %s", p.signer, *upstream)
	log.Fatal(http.ListenAndServe(*listen, p))
}

// loadKey reads the SEC1 PEM secp256k1 key at path, or at $CREDENTIALS_DIRECTORY/instance-key
// (where systemd puts the instance-key credential) when path is empty.
func loadKey(path string) (*secp256k1.PrivateKey, error) {
	if path == "" {
		dir := os.Getenv("CREDENTIALS_DIRECTORY")
		if dir == "" {
			return nil, errors.New("no instance key: CREDENTIALS_DIRECTORY is unset; " +
				"give the service InstanceKey() or pass -key FILE")
		}
		path = filepath.Join(dir, "instance-key")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read the instance key: %w", err)
	}
	for block, rest := pem.Decode(data); block != nil; block, rest = pem.Decode(rest) {
		if block.Type != "EC PRIVATE KEY" {
			continue
		}
		var sec1 ecPrivateKey
		if _, err := asn1.Unmarshal(block.Bytes, &sec1); err != nil {
			return nil, fmt.Errorf("%s: not a SEC1 EC private key: %w", path, err)
		}
		if len(sec1.Curve) > 0 && !sec1.Curve.Equal(secp256k1OID) {
			return nil, fmt.Errorf("%s: the key is on curve %v, not secp256k1", path, sec1.Curve)
		}
		if len(sec1.PrivateKey) != 32 {
			return nil, fmt.Errorf("%s: the private scalar is %d bytes, not 32", path, len(sec1.PrivateKey))
		}
		return secp256k1.PrivKeyFromBytes(sec1.PrivateKey), nil
	}
	return nil, fmt.Errorf("%s holds no EC PRIVATE KEY block (SEC1 PEM)", path)
}

type proxy struct {
	upstream string
	key      *secp256k1.PrivateKey
	signer   string
	client   *http.Client
}

func newProxy(upstream string, key *secp256k1.PrivateKey) *proxy {
	return &proxy{
		upstream: upstream,
		key:      key,
		signer:   hex.EncodeToString(key.PubKey().SerializeCompressed()),
		client:   &http.Client{Timeout: 60 * time.Second},
	}
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/signer":
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintln(w, p.signer)
	case r.Method == http.MethodPost && r.URL.Path == "/":
		p.forward(w, r)
	default:
		http.Error(w, "POST JSON-RPC to /, or GET /signer", http.StatusNotFound)
	}
}

func (p *proxy) forward(w http.ResponseWriter, r *http.Request) {
	request, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxRequest))
	if err != nil {
		http.Error(w, err.Error(), http.StatusRequestEntityTooLarge)
		return
	}
	var call struct {
		Method string `json:"method"`
	}
	sign := json.Unmarshal(request, &call) == nil && signed[call.Method]
	upstream, err := p.client.Post(p.upstream, "application/json", bytes.NewReader(request))
	if err != nil {
		http.Error(w, "nethermind: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer upstream.Body.Close()
	response, err := io.ReadAll(io.LimitReader(upstream.Body, maxResponse))
	if err != nil {
		http.Error(w, "nethermind: "+err.Error(), http.StatusBadGateway)
		return
	}
	if sign && upstream.StatusCode == http.StatusOK {
		if signature, ok := p.signResult(response); ok {
			w.Header().Set(signatureHeader, "0x"+hex.EncodeToString(signature))
			w.Header().Set(signerHeader, p.signer)
		}
	}
	w.Header().Set("Content-Type", upstream.Header.Get("Content-Type"))
	w.WriteHeader(upstream.StatusCode)
	w.Write(response)
}

// signResult signs the result of a JSON-RPC response; an error response carries none.
func (p *proxy) signResult(response []byte) ([]byte, bool) {
	var envelope struct {
		Result json.RawMessage `json:"result"`
	}
	if json.Unmarshal(response, &envelope) != nil || len(envelope.Result) == 0 {
		return nil, false
	}
	canonical, err := canonicalJSON(envelope.Result)
	if err != nil {
		return nil, false
	}
	return sign(p.key, keccak256(canonical)), true
}

// canonicalJSON re-encodes raw compact, with object keys sorted and numbers as written.
func canonicalJSON(raw []byte) ([]byte, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	encoder := json.NewEncoder(&out)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte("\n")), nil
}

func keccak256(data []byte) []byte {
	hash := sha3.NewLegacyKeccak256()
	hash.Write(data)
	return hash.Sum(nil)
}

// sign is Ethereum's recoverable signature r || s || v of hash, v = 27 + the recovery id.
func sign(key *secp256k1.PrivateKey, hash []byte) []byte {
	compact := ecdsa.SignCompact(key, hash, false)
	return append(compact[1:], compact[0])
}
