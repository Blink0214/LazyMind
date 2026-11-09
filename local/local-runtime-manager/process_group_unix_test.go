//go:build !windows

package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestProcessGroupStopHelper(t *testing.T) {
	role := os.Getenv("LAZYMIND_TEST_PROCESS_GROUP_ROLE")
	if role == "" {
		return
	}
	signal.Ignore(os.Interrupt, syscall.SIGTERM)
	if role == "parent" {
		cmd := exec.Command(os.Args[0], "-test.run=TestProcessGroupStopHelper")
		cmd.Env = append(os.Environ(), "LAZYMIND_TEST_PROCESS_GROUP_ROLE=child")
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Start(); err != nil {
			os.Exit(2)
		}
		fmt.Println(cmd.Process.Pid)
	}
	time.Sleep(30 * time.Second)
	os.Exit(0)
}

func TestStopLocalProcessRecordsKillsRecordedProcessGroup(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=TestProcessGroupStopHelper")
	cmd.Env = append(os.Environ(), "LAZYMIND_TEST_PROCESS_GROUP_ROLE=parent")
	configureChildProcess(cmd, false)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("helper stdout: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start process-group helper: %v", err)
	}
	waitDone := make(chan error, 1)
	go func() { waitDone <- cmd.Wait() }()
	defer func() {
		_ = forceKillProcessTree(cmd.Process.Pid)
		select {
		case <-waitDone:
		case <-time.After(2 * time.Second):
		}
	}()

	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("read child pid: %v", err)
	}
	childPID, err := strconv.Atoi(strings.TrimSpace(line))
	if err != nil {
		t.Fatalf("parse child pid %q: %v", line, err)
	}
	pgid := processGroupID(childPID)
	if pgid != cmd.Process.Pid {
		t.Fatalf("child process group = %d, want parent pid %d", pgid, cmd.Process.Pid)
	}

	err = stopLocalProcessRecordsWith(t.Context(), []LocalProcessRecord{{
		Service: "resource-tracker",
		PID:     childPID,
		PGID:    pgid,
	}}, processStopOptions{
		interrupt:       interruptProcess,
		forceKill:       forceKillProcessTree,
		alive:           processAlive,
		gracefulTimeout: 20 * time.Millisecond,
		forceTimeout:    2 * time.Second,
		pollInterval:    10 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("stop recorded process group: %v", err)
	}
	select {
	case <-waitDone:
	case <-time.After(2 * time.Second):
		t.Fatal("process-group parent did not exit")
	}
	if processAlive(childPID) {
		t.Fatalf("process-group child %d is still alive", childPID)
	}
}
