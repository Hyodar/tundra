package main

import (
	"fmt"
	"log/slog"
	"os"

	"github.com/Hyodar/tundra/tools/pkg/secrets"
	"github.com/spf13/cobra"
)

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))

	rootCmd := &cobra.Command{
		Use:   "secret-delivery",
		Short: "SSH key provisioning and secret delivery for TDX VMs",
	}

	setupCmd := &cobra.Command{
		Use:   "setup <config.yaml>",
		Short: "Load config, create manager, and run SSH key setup",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			configPath := args[0]

			cfg, err := secrets.LoadConfig(configPath)
			if err != nil {
				return fmt.Errorf("loading config: %w", err)
			}

			if err := cfg.Validate(); err != nil {
				return fmt.Errorf("validating config: %w", err)
			}

			provider := newProvider(cfg.SSH, logger)
			if provider == nil {
				return fmt.Errorf("unsupported strategy: %s", cfg.SSH.Strategy)
			}

			manager := secrets.NewManager(cfg.SSH, provider, nil, logger)

			if err := manager.Setup(cmd.Context()); err != nil {
				return fmt.Errorf("setup failed: %w", err)
			}

			return nil
		},
	}

	validateCmd := &cobra.Command{
		Use:   "validate <config.yaml>",
		Short: "Parse and validate a configuration file",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			configPath := args[0]

			cfg, err := secrets.LoadConfig(configPath)
			if err != nil {
				return fmt.Errorf("loading config: %w", err)
			}

			if err := cfg.Validate(); err != nil {
				return fmt.Errorf("validating config: %w", err)
			}

			logger.Info("configuration is valid", "path", configPath)
			return nil
		},
	}

	rootCmd.AddCommand(setupCmd, validateCmd)

	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}

// newProvider creates a KeyProvider based on the configured strategy.
func newProvider(cfg secrets.SSHConfig, logger *slog.Logger) secrets.KeyProvider {
	switch cfg.Strategy {
	case "webserver":
		addr := cfg.StrategyConfig["server_url"]
		return secrets.NewWebServerProvider(addr, logger)
	default:
		return nil
	}
}
