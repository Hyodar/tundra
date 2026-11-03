package attestation

import (
	"strings"
	"testing"
)

func TestRequireCaller(t *testing.T) {
	binding := []byte("binding")
	cases := []struct {
		name   string
		tagged []byte
		caller string
		refuse string
	}{
		{"the endpoint's tag over its binding", Tagged(EndpointCaller, binding), EndpointCaller, ""},
		{"a service's own tag", Tagged("signer", binding), "signer", ""},
		{"a service's quote is not the endpoint's", Tagged("signer", binding), EndpointCaller, `issued to "signer"`},
		{"untagged user data", binding, EndpointCaller, "no caller tag"},
		{"no user data", nil, EndpointCaller, "no caller tag"},
		{"another binding", Tagged(EndpointCaller, []byte("other")), EndpointCaller, `not "endpoint"'s binding`},
		{"the endpoint's tag inside a service's user data", Tagged("signer", Tagged(EndpointCaller, binding)), EndpointCaller, `issued to "signer"`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := RequireCaller(c.tagged, c.caller, binding)
			if c.refuse == "" && err != nil {
				t.Fatalf("refused: %v", err)
			}
			if c.refuse != "" && (err == nil || !strings.Contains(err.Error(), c.refuse) || !strings.Contains(err.Error(), "\nfix: ")) {
				t.Fatalf("want a refusal saying %q with a fix line, got %v", c.refuse, err)
			}
		})
	}
}
