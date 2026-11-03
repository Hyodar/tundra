// Command disk-setup manages disk encryption, formatting, and mounting for
// TDX VM initialization.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/disks"
	"github.com/Hyodar/tundra/tools/pkg/exec"
	"github.com/coreos/go-systemd/v22/daemon"
	"github.com/spf13/cobra"
)

func main() {
	root := &cobra.Command{
		Use:   "disk-setup",
		Short: "Manage disk encryption, formatting, and mounting",
	}

	root.AddCommand(setupCmd())
	root.AddCommand(validateCmd())
	root.AddCommand(ownerCmd())

	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}

func setupCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "setup <config.yaml>",
		Short: "Load config and set up all disks",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := disks.LoadConfig(args[0])
			if err != nil {
				return err
			}

			if err := cfg.Validate(); err != nil {
				return err
			}

			ctx := context.Background()
			runner := &exec.DefaultRunner{}

			// The key provider is expected to be injected by the
			// orchestrator. For standalone usage we use an
			// environment-variable-based provider as a fallback.
			kp := &envKeyProvider{}

			mgr := disks.NewManager(cfg, kp, runner)
			if err := mgr.SetupAll(ctx); err != nil {
				return err
			}

			slog.Info("all disks set up successfully")
			return nil
		},
	}
}

func ownerCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "owner <config.yaml>",
		Short: "Keep each OperatorKey() volume's owner token and the boot's owner file in step",
		Long: "Reconcile once (the header's owner becomes the boot's), tell systemd it is " +
			"ready, then reconcile again each time an owner file changes (the attested " +
			"endpoint pinned or rotated an owner), until stopped.",
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := disks.LoadConfig(args[0])
			if err != nil {
				return err
			}
			if err := cfg.Validate(); err != nil {
				return err
			}
			mgr := disks.NewManager(cfg, &envKeyProvider{}, &exec.DefaultRunner{})
			ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
			defer stop()
			return mgr.WatchOwners(ctx, 2*time.Second, func() {
				if _, err := daemon.SdNotify(false, daemon.SdNotifyReady); err != nil {
					slog.Warn("no readiness notification", "error", err)
				}
			})
		},
	}
}

func validateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "validate <config.yaml>",
		Short: "Parse and validate a disk configuration file",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := disks.LoadConfig(args[0])
			if err != nil {
				return err
			}

			if err := cfg.Validate(); err != nil {
				return err
			}

			fmt.Println("configuration is valid")
			return nil
		},
	}
}

// envKeyProvider reads the encryption key from the DISK_ENCRYPTION_KEY
// environment variable. This is a simple fallback for standalone usage;
// production deployments should inject a proper KeyProvider.
type envKeyProvider struct{}

func (p *envKeyProvider) Get(_ context.Context) (string, error) {
	key := os.Getenv("DISK_ENCRYPTION_KEY")
	if key == "" {
		return "", fmt.Errorf("DISK_ENCRYPTION_KEY environment variable is not set")
	}
	return key, nil
}
