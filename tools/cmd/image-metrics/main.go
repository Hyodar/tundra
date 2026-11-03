// Command image-metrics writes what the booted image is, and how the system is doing, as
// Prometheus text for the node exporter's textfile collector: the series an Observability's
// metrics export always keeps, so an operator tells which image a series came from.
//
//	tundravm_image_digest{image="NAME",digest="sha384:HEX"} 1  the UKI's Authenticode digest, as RTMR1's event log records it ("unmeasured" without TDX)
//	tundravm_image_rtmr{index="0",value="HEX"} 1               each RTMR the TD reports (its TDREPORT), with TDX only
//	tundravm_mode{mode="tdx"} 1                                what the execution gate recorded
//	tundravm_system_state{state="running"} 1                   systemctl is-system-running
//	tundravm_units_failed 0                                    the units in the failed state
//	tundravm_unit_active{unit="box.service"} 1                 each loaded service, 1 while active
//	tundravm_unit_cpu_seconds_total{unit="box.service"} 1.5    the CPU time systemd accounts to its cgroup
//
// The per-unit series are what recording rules derive liveness and CPU booleans from: a
// container's processes are all in its unit's cgroup. The identity is read once, the state
// every interval. At start it also prints the
// identity to the journal, one line for the image and one per RTMR:
//
//	image fixture, digest sha384:HEX, mode tdx
//	RTMR0 HEX
//
// It runs as root without capabilities: the event log and the TDX device are root's.
//
// Usage: image-metrics [-interval 30s] OUTPUT
package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	osRelease = "/etc/os-release"
	modeFile  = "/run/tundravm/mode"
	ccelPath  = "/sys/firmware/acpi/tables/data/CCEL"
	tdxGuest  = "/dev/tdx_guest"

	// _IOWR('T', 1, struct tdx_report_req): 64 bytes of report data, 1024 of TDREPORT.
	tdxGetReport0 = 0xc4405401
	// TDINFO starts at 512 in the TDREPORT; its RTMRs follow ATTRIBUTES, XFAM, MRTD,
	// MRCONFIGID, MROWNER and MROWNERCONFIG.
	rtmrOffset = 512 + 8 + 8 + 4*48
	rtmrSize   = 48

	algSHA384                 = 0x000c
	evBootServicesApplication = 0x80000003
	// The CCEL's MR index of RTMR1 (0 is MRTD).
	ccelRTMR1 = 2
)

type identity struct {
	image  string
	digest string
	rtmrs  []string
	mode   string
}

func main() {
	interval := flag.Duration("interval", 30*time.Second, "how often the system state is read")
	flag.Parse()
	if flag.NArg() != 1 {
		log.Fatal("usage: image-metrics [-interval 30s] OUTPUT")
	}
	output := flag.Arg(0)
	id := readIdentity()
	for _, line := range id.lines() {
		fmt.Println(line)
	}
	for {
		state, failed := systemState()
		if err := write(output, render(id, state, failed, serviceUnits())); err != nil {
			log.Fatal(err)
		}
		time.Sleep(*interval)
	}
}

func readIdentity() identity {
	id := identity{image: imageName(osRelease), digest: "unmeasured", mode: "unknown"}
	if mode, err := os.ReadFile(modeFile); err == nil {
		id.mode = strings.TrimSpace(string(mode))
	}
	if eventLog, err := os.ReadFile(ccelPath); err == nil {
		digest, err := ukiDigest(eventLog)
		if err != nil {
			log.Printf("%s: %v", ccelPath, err)
		} else {
			id.digest = "sha384:" + hex.EncodeToString(digest)
		}
	}
	if _, err := os.Stat(tdxGuest); err == nil {
		report, err := tdReport(tdxGuest)
		if err != nil {
			log.Printf("%s: %v", tdxGuest, err)
		} else {
			id.rtmrs = reportRTMRs(report)
		}
	}
	return id
}

// imageName is the os-release IMAGE_ID mkosi writes: the image's name.
func imageName(path string) string {
	file, err := os.Open(path)
	if err != nil {
		return "unknown"
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		if value, found := strings.CutPrefix(scanner.Text(), "IMAGE_ID="); found {
			return strings.Trim(value, `"'`)
		}
	}
	return "unknown"
}

// ukiDigest is the SHA-384 of the first EV_EFI_BOOT_SERVICES_APPLICATION event in RTMR1
// of a CCEL (a TCG2 crypto-agile event log): the UKI's Authenticode digest.
func ukiDigest(eventLog []byte) ([]byte, error) {
	truncated := errors.New("the event log is truncated")
	if len(eventLog) < 32 {
		return nil, truncated
	}
	header := 32 + int(le32(eventLog[28:]))
	if header > len(eventLog) {
		return nil, truncated
	}
	sizes, err := algorithms(eventLog[32:header])
	if err != nil {
		return nil, err
	}
	for at := header; at+12 <= len(eventLog); {
		index, kind, count := le32(eventLog[at:]), le32(eventLog[at+4:]), int(le32(eventLog[at+8:]))
		if index == 0xffffffff || count == 0 || count > len(sizes) {
			break // the table's unused tail
		}
		at += 12
		var sha384 []byte
		for range count {
			if at+2 > len(eventLog) {
				return nil, truncated
			}
			alg := binary.LittleEndian.Uint16(eventLog[at:])
			size, known := sizes[alg]
			if !known {
				return nil, fmt.Errorf("an event uses algorithm %#04x, which the header does not list", alg)
			}
			if at+2+size > len(eventLog) {
				return nil, truncated
			}
			if alg == algSHA384 {
				sha384 = eventLog[at+2 : at+2+size]
			}
			at += 2 + size
		}
		if at+4 > len(eventLog) {
			return nil, truncated
		}
		at += 4 + int(le32(eventLog[at:]))
		if at > len(eventLog) {
			return nil, truncated
		}
		if index == ccelRTMR1 && kind == evBootServicesApplication && sha384 != nil {
			return sha384, nil
		}
	}
	return nil, errors.New("no EV_EFI_BOOT_SERVICES_APPLICATION event in RTMR1")
}

// algorithms reads the digest sizes a "Spec ID Event03" header event lists.
func algorithms(spec []byte) (map[uint16]int, error) {
	if len(spec) < 28 || !bytes.HasPrefix(spec, []byte("Spec ID Event03")) {
		return nil, errors.New("the event log does not start with a Spec ID Event03 header")
	}
	count := int(le32(spec[24:]))
	if 28+4*count > len(spec) {
		return nil, errors.New("the Spec ID Event03 header is truncated")
	}
	sizes := make(map[uint16]int, count)
	for i := range count {
		entry := spec[28+4*i:]
		sizes[binary.LittleEndian.Uint16(entry)] = int(binary.LittleEndian.Uint16(entry[2:]))
	}
	return sizes, nil
}

// tdReport asks the TDX module for a TDREPORT with zero report data.
func tdReport(device string) ([]byte, error) {
	file, err := os.OpenFile(device, os.O_RDWR, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	var request [64 + 1024]byte
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL, file.Fd(), tdxGetReport0, uintptr(unsafe.Pointer(&request[0])),
	)
	if errno != 0 {
		return nil, fmt.Errorf("TDX_CMD_GET_REPORT0: %w", errno)
	}
	return request[64:], nil
}

// lines is the identity as the journal lines printed at start: the image, then RTMR0..3,
// or why there are none.
func (id identity) lines() []string {
	found := []string{fmt.Sprintf("image %s, digest %s, mode %s", id.image, id.digest, id.mode)}
	if len(id.rtmrs) == 0 {
		return append(found, "RTMRs unread: no TDX report")
	}
	for i, value := range id.rtmrs {
		found = append(found, fmt.Sprintf("RTMR%d %s", i, value))
	}
	return found
}

// reportRTMRs is RTMR0..3 of a TDREPORT, in hex.
func reportRTMRs(report []byte) []string {
	found := make([]string, 4)
	for i := range found {
		at := rtmrOffset + i*rtmrSize
		found[i] = hex.EncodeToString(report[at : at+rtmrSize])
	}
	return found
}

// systemState is systemctl's word for the system and the number of failed units.
func systemState() (string, int) {
	said, _ := exec.Command("systemctl", "is-system-running").Output()
	state := strings.TrimSpace(string(said))
	if state == "" {
		state = "unknown"
	}
	listed, _ := exec.Command("systemctl", "list-units", "--state=failed", "--no-legend", "--plain").Output()
	failed := 0
	for _, line := range strings.Split(string(listed), "\n") {
		if strings.TrimSpace(line) != "" {
			failed++
		}
	}
	return state, failed
}

// unit is one loaded service as systemctl shows it; cpu is negative when unaccounted.
type unit struct {
	name   string
	active bool
	cpu    float64
}

// serviceUnits is every loaded service with its state and CPU time.
func serviceUnits() []unit {
	listed, _ := exec.Command("systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain").Output()
	args := []string{"show", "-p", "Id,LoadState,ActiveState,CPUUsageNSec", "--"}
	for _, line := range strings.Split(string(listed), "\n") {
		if fields := strings.Fields(line); len(fields) > 0 && strings.HasSuffix(fields[0], ".service") {
			args = append(args, fields[0])
		}
	}
	if len(args) == 5 {
		return nil
	}
	shown, _ := exec.Command("systemctl", args...).Output()
	return parseUnits(string(shown))
}

// parseUnits reads systemctl show's blocks of Id, LoadState, ActiveState and CPUUsageNSec,
// keeping the loaded units.
func parseUnits(shown string) []unit {
	var found []unit
	for _, block := range strings.Split(shown, "\n\n") {
		props := map[string]string{}
		for _, line := range strings.Split(block, "\n") {
			if key, value, ok := strings.Cut(line, "="); ok {
				props[key] = value
			}
		}
		if props["Id"] == "" || props["LoadState"] != "loaded" {
			continue
		}
		u := unit{name: props["Id"], active: props["ActiveState"] == "active", cpu: -1}
		if nsec, err := strconv.ParseUint(props["CPUUsageNSec"], 10, 64); err == nil && nsec != math.MaxUint64 {
			u.cpu = float64(nsec) / 1e9
		}
		found = append(found, u)
	}
	return found
}

func render(id identity, state string, failed int, units []unit) []byte {
	var out strings.Builder
	gauge := func(name, help string) {
		fmt.Fprintf(&out, "# HELP %s %s\n# TYPE %s gauge\n", name, help, name)
	}
	gauge("tundravm_image_digest", "The image this VM booted: the UKI's Authenticode SHA-384 RTMR1's event log records.")
	fmt.Fprintf(&out, "tundravm_image_digest{image=%q,digest=%q} 1\n", id.image, id.digest)
	if len(id.rtmrs) > 0 {
		gauge("tundravm_image_rtmr", "The RTMRs the TD reports.")
		for i, value := range id.rtmrs {
			fmt.Fprintf(&out, "tundravm_image_rtmr{index=\"%d\",value=%q} 1\n", i, value)
		}
	}
	gauge("tundravm_mode", "What the execution gate recorded: tdx or simulated.")
	fmt.Fprintf(&out, "tundravm_mode{mode=%q} 1\n", id.mode)
	gauge("tundravm_system_state", "What systemctl is-system-running says.")
	fmt.Fprintf(&out, "tundravm_system_state{state=%q} 1\n", state)
	gauge("tundravm_units_failed", "The units in the failed state.")
	fmt.Fprintf(&out, "tundravm_units_failed %d\n", failed)
	if len(units) > 0 {
		gauge("tundravm_unit_active", "Each loaded service: 1 while active.")
		for _, u := range units {
			active := 0
			if u.active {
				active = 1
			}
			fmt.Fprintf(&out, "tundravm_unit_active{unit=%q} %d\n", u.name, active)
		}
		fmt.Fprintf(&out, "# HELP tundravm_unit_cpu_seconds_total The CPU time systemd accounts to each service's cgroup.\n# TYPE tundravm_unit_cpu_seconds_total counter\n")
		for _, u := range units {
			if u.cpu >= 0 {
				fmt.Fprintf(&out, "tundravm_unit_cpu_seconds_total{unit=%q} %g\n", u.name, u.cpu)
			}
		}
	}
	return []byte(out.String())
}

// write replaces path with data, readable by the node exporter's user.
func write(path string, data []byte) error {
	scratch, err := os.CreateTemp(filepath.Dir(path), ".image-metrics-*")
	if err != nil {
		return err
	}
	defer os.Remove(scratch.Name())
	if _, err := scratch.Write(data); err != nil {
		scratch.Close()
		return err
	}
	if err := scratch.Chmod(0o644); err != nil {
		scratch.Close()
		return err
	}
	if err := scratch.Close(); err != nil {
		return err
	}
	return os.Rename(scratch.Name(), path)
}

func le32(b []byte) uint32 { return binary.LittleEndian.Uint32(b) }
