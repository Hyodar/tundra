package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/decred/dcrd/dcrec/secp256k1/v4"
	"github.com/decred/dcrd/dcrec/secp256k1/v4/ecdsa"
)

// testKey stands in for the instance key: SEC1 PEM from `openssl ecparam -name secp256k1 -genkey -noout`.
const testKey = `-----BEGIN EC PRIVATE KEY-----
MHQCAQEEIIBAaXyqIPlNF+prwxpTtmUD6+jmigX894MsblmWq1ogoAcGBSuBBAAK
oUQDQgAEMs05EBI37Pm+D/efiCf1o9/w6c4vluOu4qTK23AuulEOW9eR8DHp8Yxf
6ydlrBXkLMU8R6rIexxLpxImBxuiJQ==
-----END EC PRIVATE KEY-----
`

func newSigner(t *testing.T) (*httptest.Server, string) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "instance-key"), []byte(testKey), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CREDENTIALS_DIRECTORY", dir)
	key, err := loadKey("")
	if err != nil {
		t.Fatal(err)
	}
	nethermind := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		if bytes.Contains(body, []byte("eth_getBlockByNumber")) {
			io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":{"number": "0x1", "hash": "0xab"}}`)
			return
		}
		io.WriteString(w, `{"jsonrpc":"2.0","id":1,"result":"0x88bb0"}`)
	}))
	t.Cleanup(nethermind.Close)
	signer := httptest.NewServer(newProxy(nethermind.URL, key))
	t.Cleanup(signer.Close)
	return signer, hex.EncodeToString(key.PubKey().SerializeCompressed())
}

func call(t *testing.T, signer *httptest.Server, method, path, body, nonce string) (*http.Response, []byte) {
	request, _ := http.NewRequest(method, signer.URL+path, strings.NewReader(body))
	if nonce != "" {
		request.Header.Set(nonceHeader, nonce)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	answer, _ := io.ReadAll(response.Body)
	return response, answer
}

const blockRequest = `{"jsonrpc":"2.0","id":1,"method":"eth_getBlockByNumber","params":["0x1", false]}`

func TestProxy(t *testing.T) {
	signer, public := newSigner(t)
	cases := []struct {
		name, method, path, body, nonce string
		status                          int
	}{
		{"a block request without a nonce is refused", http.MethodPost, "/", blockRequest, "", http.StatusBadRequest},
		{"a short nonce is refused", http.MethodPost, "/", blockRequest, "abcd", http.StatusBadRequest},
		{"a nonce that is not hex is refused", http.MethodPost, "/", blockRequest, strings.Repeat("zz", 32), http.StatusBadRequest},
		{"another method passes through unsigned", http.MethodPost, "/",
			`{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}`, "", http.StatusOK},
		{"GET /signer answers the public key", http.MethodGet, "/signer", "", "", http.StatusOK},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			response, body := call(t, signer, c.method, c.path, c.body, c.nonce)
			if response.StatusCode != c.status {
				t.Fatalf("status %d, want %d: %s", response.StatusCode, c.status, body)
			}
			if c.path == "/signer" && strings.TrimSpace(string(body)) != public {
				t.Fatalf("GET /signer = %q, want %q", body, public)
			}
			if response.Header.Get(signatureHeader) != "" || response.Header.Get(signerHeader) != "" {
				t.Fatalf("carries a signature: %q", response.Header.Get(signatureHeader))
			}
		})
	}
}

func TestSignatureBindsTheExchange(t *testing.T) {
	signer, public := newSigner(t)
	nonce := strings.Repeat("ab", 32)
	response, body := call(t, signer, http.MethodPost, "/", blockRequest, strings.ToUpper(nonce))
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status %d: %s", response.StatusCode, body)
	}
	if got := response.Header.Get(signerHeader); got != public {
		t.Fatalf("%s = %q, want %q", signerHeader, got, public)
	}
	if got := response.Header.Get(nonceHeader); got != nonce {
		t.Fatalf("%s = %q, want the nonce as lowercase hex %q", nonceHeader, got, nonce)
	}
	signature := response.Header.Get(signatureHeader)
	raw, err := hex.DecodeString(strings.TrimPrefix(signature, "0x"))
	if err != nil || len(raw) != 65 || (raw[64] != 27 && raw[64] != 28) {
		t.Fatalf("%s = %q is not r || s || v", signatureHeader, signature)
	}

	message := func(method, nonce, params, result string) string {
		return `{"method":"` + method + `","nonce":"` + nonce + `","params":` + params + `,"result":` + result + `}`
	}
	block := `{"hash":"0xab","number":"0x1"}`
	cases := []struct {
		name, message string
		verifies      bool
	}{
		{"it verifies for its own request", message("eth_getBlockByNumber", nonce, `["0x1",false]`, block), true},
		{"it fails for another method", message("eth_getBlockByHash", nonce, `["0x1",false]`, block), false},
		{"it fails for other params", message("eth_getBlockByNumber", nonce, `["0x2",false]`, block), false},
		{"it fails for another result", message("eth_getBlockByNumber", nonce, `["0x1",false]`, `{"hash":"0xcd","number":"0x1"}`), false},
		{"it fails for another nonce", message("eth_getBlockByNumber", strings.Repeat("cd", 32), `["0x1",false]`, block), false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			hash := keccak256([]byte(c.message))
			recovered, _, err := ecdsa.RecoverCompact(append([]byte{raw[64]}, raw[:64]...), hash)
			if (err == nil && hex.EncodeToString(recovered.SerializeCompressed()) == public) != c.verifies {
				t.Fatalf("ecrecover over %s recovers the signer: %v, want %v", c.message, !c.verifies, c.verifies)
			}
			published, _ := hex.DecodeString(public)
			key, err := secp256k1.ParsePubKey(published)
			if err != nil {
				t.Fatal(err)
			}
			var r, s secp256k1.ModNScalar
			r.SetByteSlice(raw[:32])
			s.SetByteSlice(raw[32:64])
			if ecdsa.NewSignature(&r, &s).Verify(hash, key) != c.verifies {
				t.Fatalf("Verify over %s, want %v", c.message, c.verifies)
			}
		})
	}
}

func TestQuote(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "tdxs.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	asked := make(chan issueRequest, 4)
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			var request issueRequest
			json.NewDecoder(conn).Decode(&request)
			asked <- request
			document := hex.EncodeToString([]byte(`{"quote":"q"}`))
			io.WriteString(conn, `{"data":{"document":"`+document+`"},"error":null}`+"\n")
			conn.Close()
		}
	}()
	key, _ := secp256k1.GeneratePrivateKey()
	p := newProxy("http://127.0.0.1:1/", key)
	p.evidence = &evidence{socket: socket, signer: key.PubKey().SerializeCompressed()}
	if err := p.evidence.check(); err != nil {
		t.Fatal(err)
	}
	if got := (<-asked).Data.UserData; got != p.signer {
		t.Fatalf("the start check quoted %s, not the key %s", got, p.signer)
	}
	signer := httptest.NewServer(p)
	defer signer.Close()

	cases := []struct {
		name, nonce string
		status      int
	}{
		{"a nonce is quoted with the key", strings.Repeat("ab", 32), http.StatusOK},
		{"a short nonce is refused", "abcd", http.StatusBadRequest},
		{"a nonce that is not hex is refused", strings.Repeat("zz", 32), http.StatusBadRequest},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			response, err := http.Get(signer.URL + "/quote?nonce=" + c.nonce)
			if err != nil {
				t.Fatal(err)
			}
			var body struct {
				Signer   string          `json:"signer"`
				Document json.RawMessage `json:"document"`
			}
			json.NewDecoder(response.Body).Decode(&body)
			response.Body.Close()
			if response.StatusCode != c.status {
				t.Fatalf("status %d, want %d", response.StatusCode, c.status)
			}
			if c.status != http.StatusOK {
				return
			}
			request := <-asked
			if request.Data.Nonce != c.nonce || request.Data.UserData != p.signer {
				t.Fatalf("asked for %+v", request.Data)
			}
			if body.Signer != p.signer || string(body.Document) != `{"quote":"q"}` {
				t.Fatalf("answered %s %s", body.Signer, body.Document)
			}
		})
	}
}
