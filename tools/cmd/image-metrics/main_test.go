package main

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"strings"
	"testing"
)

// event is one TCG_PCR_EVENT2 with a SHA-384 digest of fill.
func event(index, kind uint32, fill byte) []byte {
	var b bytes.Buffer
	binary.Write(&b, binary.LittleEndian, []uint32{index, kind, 1})
	binary.Write(&b, binary.LittleEndian, uint16(algSHA384))
	b.Write(bytes.Repeat([]byte{fill}, 48))
	binary.Write(&b, binary.LittleEndian, uint32(3))
	b.WriteString("abc")
	return b.Bytes()
}

// eventLog is a CCEL: the SHA-1 header event with a Spec ID Event03 listing SHA-384, then events.
func eventLog(events ...[]byte) []byte {
	var spec bytes.Buffer
	spec.WriteString("Spec ID Event03\x00")
	binary.Write(&spec, binary.LittleEndian, []uint32{0, 0x00020000, 1})
	binary.Write(&spec, binary.LittleEndian, []uint16{algSHA384, 48})
	spec.WriteByte(0)
	var b bytes.Buffer
	binary.Write(&b, binary.LittleEndian, []uint32{0, 3})
	b.Write(make([]byte, 20))
	binary.Write(&b, binary.LittleEndian, uint32(spec.Len()))
	b.Write(spec.Bytes())
	for _, e := range events {
		b.Write(e)
	}
	return b.Bytes()
}

func TestUKIDigest(t *testing.T) {
	uki := strings.Repeat("bb", 48)
	cases := []struct {
		name string
		log  []byte
		want string
		err  string
	}{
		{"first boot application in RTMR1", eventLog(event(1, 0x8000000a, 0xaa), event(2, evBootServicesApplication, 0xbb), event(2, evBootServicesApplication, 0xcc)), uki, ""},
		{"padding ends the log", append(eventLog(event(2, evBootServicesApplication, 0xbb)), bytes.Repeat([]byte{0xff}, 64)...), uki, ""},
		{"none in RTMR1", eventLog(event(1, evBootServicesApplication, 0xaa)), "", "no EV_EFI_BOOT_SERVICES_APPLICATION"},
		{"truncated", eventLog(event(2, evBootServicesApplication, 0xbb))[:120], "", "truncated"},
		{"no spec header", make([]byte, 64), "", "Spec ID Event03"},
	}
	for _, c := range cases {
		got, err := ukiDigest(c.log)
		if c.err != "" {
			if err == nil || !strings.Contains(err.Error(), c.err) {
				t.Errorf("%s: error %v, want %q", c.name, err, c.err)
			}
			continue
		}
		if err != nil || hex.EncodeToString(got) != c.want {
			t.Errorf("%s: %x, %v; want %s", c.name, got, err, c.want)
		}
	}
}

func TestRender(t *testing.T) {
	report := make([]byte, 1024)
	for i := range 4 {
		report[rtmrOffset+i*rtmrSize] = byte(i + 1)
	}
	cases := []struct {
		name string
		id   identity
		want []string
		not  string
	}{
		{
			"tdx",
			identity{image: "fixture", digest: "sha384:bb", rtmrs: reportRTMRs(report), mode: "tdx"},
			[]string{
				`tundravm_image_digest{image="fixture",digest="sha384:bb"} 1`,
				`tundravm_image_rtmr{index="2",value="03` + strings.Repeat("00", 47) + `"} 1`,
				`tundravm_mode{mode="tdx"} 1`,
				`tundravm_system_state{state="running"} 1`,
				`tundravm_units_failed 0`,
			},
			"",
		},
		{
			"simulated",
			identity{image: "fixture", digest: "unmeasured", mode: "simulated"},
			[]string{`tundravm_image_digest{image="fixture",digest="unmeasured"} 1`, `tundravm_mode{mode="simulated"} 1`},
			"tundravm_image_rtmr",
		},
	}
	for _, c := range cases {
		text := string(render(c.id, "running", 0))
		for _, line := range c.want {
			if !strings.Contains(text, line+"\n") {
				t.Errorf("%s: no %q in\n%s", c.name, line, text)
			}
		}
		if c.not != "" && strings.Contains(text, c.not) {
			t.Errorf("%s: %q in\n%s", c.name, c.not, text)
		}
	}
}

func TestIdentityLines(t *testing.T) {
	report := make([]byte, 1024)
	report[rtmrOffset+3*rtmrSize] = 0x04
	cases := []struct {
		name string
		id   identity
		want []string
	}{
		{
			"tdx",
			identity{image: "fixture", digest: "sha384:bb", rtmrs: reportRTMRs(report), mode: "tdx"},
			[]string{
				"image fixture, digest sha384:bb, mode tdx",
				"RTMR0 " + strings.Repeat("00", 48),
				"RTMR1 " + strings.Repeat("00", 48),
				"RTMR2 " + strings.Repeat("00", 48),
				"RTMR3 04" + strings.Repeat("00", 47),
			},
		},
		{
			"simulated",
			identity{image: "fixture", digest: "unmeasured", mode: "simulated"},
			[]string{"image fixture, digest unmeasured, mode simulated", "RTMRs unread: no TDX report"},
		},
	}
	for _, c := range cases {
		if got := c.id.lines(); strings.Join(got, "\n") != strings.Join(c.want, "\n") {
			t.Errorf("%s: %q, want %q", c.name, got, c.want)
		}
	}
}
