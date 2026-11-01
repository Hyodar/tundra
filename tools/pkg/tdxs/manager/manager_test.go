package manager

import (
	"testing"

	"gopkg.in/yaml.v3"
)

type nopLogger struct{}

func (nopLogger) Debug(msg string, args ...any) {}
func (nopLogger) Info(msg string, args ...any)  {}
func (nopLogger) Warn(msg string, args ...any)  {}
func (nopLogger) Error(msg string, args ...any) {}

func TestManagerConfig_SimulatorRoundTrip(t *testing.T) {
	configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test-tdxs.sock
issuer:
  type: simulator
validator:
  type: simulator
`

	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
		t.Fatalf("failed to unmarshal config: %v", err)
	}

	mgr, err := NewManager(&cfg, nopLogger{})
	if err != nil {
		t.Fatalf("failed to create manager: %v", err)
	}
	if mgr == nil {
		t.Fatal("expected non-nil manager")
	}
}

func TestManagerConfig_AllIssuerTypes(t *testing.T) {
	types := []string{"simulator"}
	// tdx, azure, gcp require hardware and will fail at issuer creation,
	// but their config parsing should succeed.
	for _, issuerType := range types {
		configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test-tdxs.sock
issuer:
  type: ` + issuerType + `
`
		var cfg ManagerConfig
		if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
			t.Fatalf("failed to unmarshal config for issuer type %s: %v", issuerType, err)
		}

		if cfg.Issuer == nil {
			t.Fatalf("expected non-nil issuer config for type %s", issuerType)
		}
	}
}

func TestManagerConfig_AllValidatorTypes(t *testing.T) {
	types := []string{"simulator", "azure", "gcp", "tdx"}
	for _, validatorType := range types {
		configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test-tdxs.sock
validator:
  type: ` + validatorType + `
`
		var cfg ManagerConfig
		if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
			t.Fatalf("failed to unmarshal config for validator type %s: %v", validatorType, err)
		}

		if cfg.Validator == nil {
			t.Fatalf("expected non-nil validator config for type %s", validatorType)
		}
	}
}

func TestManagerConfig_InvalidTransportType(t *testing.T) {
	configYAML := `
transport:
  type: invalid
  config:
    file_path: /tmp/test.sock
issuer:
  type: simulator
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err == nil {
		t.Fatal("expected error for invalid transport type")
	}
}

func TestManagerConfig_InvalidIssuerType(t *testing.T) {
	configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test.sock
issuer:
  type: invalid
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err == nil {
		t.Fatal("expected error for invalid issuer type")
	}
}

func TestManagerConfig_InvalidValidatorType(t *testing.T) {
	configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test.sock
validator:
  type: invalid
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err == nil {
		t.Fatal("expected error for invalid validator type")
	}
}

func TestManagerConfig_ValidatorWithMeasurements(t *testing.T) {
	configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test-tdxs.sock
validator:
  type: tdx
  config:
    expected_measurements:
      mrtd: "abc123"
      rtmr0: "def456"
    check_revocations: true
    get_collateral: false
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
		t.Fatalf("failed to unmarshal config: %v", err)
	}

	if cfg.Validator == nil {
		t.Fatal("expected non-nil validator config")
	}
}

func TestManagerConfig_MissingTransport(t *testing.T) {
	configYAML := `
issuer:
  type: simulator
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	_, err := NewManager(&cfg, nopLogger{})
	if err == nil {
		t.Fatal("expected error for missing transport config")
	}
}

func TestManagerConfig_MissingIssuerAndValidator(t *testing.T) {
	configYAML := `
transport:
  type: socket
  config:
    file_path: /tmp/test.sock
`
	var cfg ManagerConfig
	if err := yaml.Unmarshal([]byte(configYAML), &cfg); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	_, err := NewManager(&cfg, nopLogger{})
	if err == nil {
		t.Fatal("expected error when both issuer and validator are nil")
	}
}
