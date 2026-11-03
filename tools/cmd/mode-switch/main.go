// mode-switch puts a firewall mode in force: it takes a mode's name from the attested
// endpoint on a root-only unix socket, extends RTMR3 with the switch's event, appends it to
// the boot's log and loads the mode's nft transaction, and it closes a window once the mode
// has lasted its time on CLOCK_BOOTTIME. It opens no network socket.
//
// mode-switch status prints the mode in force, the switches since boot and whether the
// RTMR3 the evidence service reports is the log's replay; it exits 1 when it is not.
package main

import (
	"bufio"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/endpoint"
	"github.com/Hyodar/tundra/tools/pkg/modes"
	"github.com/coreos/go-systemd/v22/daemon"
	"github.com/spf13/cobra"
	"golang.org/x/sys/unix"
)

// gateMode is what the execution gate recorded: tdx or simulated.
const gateMode = "/run/tundravm/mode"

const quoteSocket = "/var/tdxs.sock"

func say(format string, args ...any) {
	fmt.Printf("mode-switch: "+format+"\n", args...)
}

func boottime() int64 {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_BOOTTIME, &ts); err != nil {
		panic(err)
	}
	return ts.Sec
}

// extender extends the real RTMR3 under TDX; under the simulator the evidence service
// replays the log instead, and with no recorded mode nothing switches.
func extender() func([]byte) error {
	data, err := os.ReadFile(gateMode)
	mode := strings.TrimSpace(string(data))
	switch {
	case err == nil && mode == "tdx":
		return func(digest []byte) error {
			f, err := os.OpenFile(modes.RTMR3Path, os.O_WRONLY, 0)
			if err != nil {
				return err
			}
			if _, err := f.Write(digest); err != nil {
				f.Close()
				return err
			}
			return f.Close()
		}
	case err == nil && mode == "simulated":
		return func([]byte) error { return nil }
	}
	return func([]byte) error { return fmt.Errorf("the execution gate recorded no mode in %s", gateMode) }
}

func apply(batch string) error {
	cmd := exec.Command("nft", "-f", "-")
	cmd.Stdin = strings.NewReader(batch)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nft: %v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

type reply struct {
	Data  *modes.Result `json:"data,omitempty"`
	Error string        `json:"error,omitempty"`
}

func announce(r modes.Result) {
	closes := ""
	if r.Until > 0 {
		closes = fmt.Sprintf(", closes at %ds", r.Until)
	}
	say("%s at %ds of boot (switch %d, from %s%s), extended into RTMR3", r.Mode, r.At, r.Switches, r.Previous, closes)
}

// handle takes one request line and answers it; a switch whose ruleset did not load
// stops the daemon, whose restart loads the mode the log ends in.
func handle(conn net.Conn, s *modes.Switcher, fatal chan<- error) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(30 * time.Second))
	line, err := bufio.NewReader(conn).ReadBytes('\n')
	var req struct {
		Mode string `json:"mode"`
	}
	if err != nil || json.Unmarshal(line, &req) != nil {
		_ = json.NewEncoder(conn).Encode(reply{Error: "the request is not one JSON line naming a mode"})
		return
	}
	result, err := s.Switch(req.Mode)
	if err != nil {
		var problem *modes.Problem
		say("refused %s: %v", req.Mode, strings.ReplaceAll(err.Error(), "\n", "; "))
		_ = json.NewEncoder(conn).Encode(reply{Error: err.Error()})
		if !errors.As(err, &problem) {
			fatal <- err
		}
		return
	}
	announce(result)
	_ = json.NewEncoder(conn).Encode(reply{Data: &result})
}

func serve(config string) error {
	cfg, err := modes.LoadConfig(config)
	if err != nil {
		return err
	}
	s := &modes.Switcher{Config: cfg, Events: modes.EventsPath, Extend: extender(), Apply: apply, Clock: boottime}
	current, err := s.Restore()
	if err != nil {
		return err
	}
	say("%s in force (modes %s)", current, strings.Join(cfg.Names(), ", "))
	_ = os.Remove(modes.SocketPath)
	listener, err := net.Listen("unix", modes.SocketPath)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(modes.SocketPath, 0o600); err != nil {
		return err
	}
	_, _ = daemon.SdNotify(false, daemon.SdNotifyReady)
	fatal := make(chan error, 1)
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				fatal <- err
				return
			}
			go handle(conn, s, fatal)
		}
	}()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	for {
		select {
		case <-ticker.C:
			closed, err := s.Expire()
			if err != nil {
				return err
			}
			if closed != nil {
				announce(*closed)
			}
		case err := <-fatal:
			return err
		case <-signals:
			return nil
		}
	}
}

func status(config string) error {
	cfg, err := modes.LoadConfig(config)
	if err != nil {
		return err
	}
	events, err := modes.ReadEvents(modes.EventsPath)
	if err != nil {
		return err
	}
	head := fmt.Sprintf("%s, %d switches since boot", modes.Current(events), len(events))
	if _, ok := cfg.Find(modes.Current(events)); !ok {
		return fmt.Errorf("%s: the log ends in a mode the image does not declare", head)
	}
	data, err := endpoint.SocketQuoter{Path: quoteSocket}.Metadata(context.Background())
	if err != nil {
		return fmt.Errorf("%s, RTMR3 unread: %w", head, err)
	}
	var meta struct {
		Metadata struct {
			Rtmr3 string `json:"rtmr3"`
		} `json:"metadata"`
	}
	if err := json.Unmarshal(data, &meta); err != nil || meta.Metadata.Rtmr3 == "" {
		return fmt.Errorf("%s, RTMR3 unread: the evidence service reported none", head)
	}
	replayed := hex.EncodeToString(modes.Replay(modes.Lines(events)))
	if !strings.EqualFold(meta.Metadata.Rtmr3, replayed) {
		return fmt.Errorf("%s, RTMR3 %s is not the log's replay %s", head, meta.Metadata.Rtmr3, replayed)
	}
	fmt.Printf("%s, RTMR3 replays the log\n", head)
	return nil
}

func main() {
	var config string
	root := &cobra.Command{
		Use:           "mode-switch",
		Short:         "Put a firewall mode in force, extended into RTMR3",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE:          func(*cobra.Command, []string) error { return serve(config) },
	}
	root.PersistentFlags().StringVarP(&config, "config", "c", modes.ConfigPath, "modes config path")
	root.AddCommand(&cobra.Command{
		Use:   "status",
		Short: "The mode in force, the switches since boot, and whether RTMR3 replays them",
		Args:  cobra.NoArgs,
		RunE:  func(*cobra.Command, []string) error { return status(config) },
	})
	if err := root.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "mode-switch: %v\n", err)
		os.Exit(1)
	}
}
