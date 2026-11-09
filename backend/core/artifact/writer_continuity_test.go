package artifact

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gorilla/mux"
	"lazymind/core/doc"
)

func TestWriterConversationEditRetryRestoreAndDownload(t *testing.T) {
	svc := New(v2TestDB(t).DB)
	ctx := context.Background()
	t.Setenv("LAZYMIND_ARTIFACT_STORAGE_ROOT", t.TempDir())
	t.Setenv("LAZYMIND_FILE_URL_SIGN_SECRET", "writer-continuity-test")
	firstRequest := CommitRequest{
		OwnerUserID: "writer", LogicalKey: "conv:writing-session:report", Title: "report.md",
		ContentType: "file", Content: []byte("# Report\n\nFirst draft.\n\n[Source](https://example.org/evidence)\n"),
		IdempotencyKey: "writing-session/generate-1",
		Bindings: []BindingSpec{
			{ScopeType: ScopeConversation, ScopeID: "writing-session", Role: RoleOutput, FollowHead: true},
			{ScopeType: ScopeHistory, ScopeID: "writing-session/round-1", Role: RoleOutput},
		},
	}
	first, err := svc.CommitRevision(ctx, firstRequest)
	if err != nil {
		t.Fatal(err)
	}
	edit := firstRequest
	edit.ArtifactID, edit.BaseRevisionID, edit.ExpectedHeadVer = first.ArtifactID, first.RevisionID, first.HeadVersion
	edit.IdempotencyKey = "writing-session/edit-2"
	edit.Bindings = []BindingSpec{firstRequest.Bindings[0], {ScopeType: ScopeHistory, ScopeID: "writing-session/round-2", Role: RoleOutput}}
	edit.Content = []byte("# Report\n\nEdited draft.\n\n[Source](https://example.org/evidence)\n")
	second, err := svc.CommitRevision(ctx, edit)
	if err != nil {
		t.Fatal(err)
	}
	retry, err := svc.CommitRevision(ctx, edit)
	if err != nil || retry.RevisionID != second.RevisionID {
		t.Fatalf("retry: %#v %v", retry, err)
	}
	stale := edit
	stale.IdempotencyKey = "writing-session/stale-edit"
	stale.Content = []byte("must not publish")
	if _, err := svc.CommitRevision(ctx, stale); err != ErrRevisionConflict {
		t.Fatalf("stale edit: %v", err)
	}
	head, err := svc.Head(ctx, first.ArtifactID, ChannelPublished)
	if err != nil || head.RevisionID != second.RevisionID {
		t.Fatalf("failed edit changed head: %#v %v", head, err)
	}
	restored, err := svc.RestorePublished(ctx, "writer", first.ArtifactID, first.RevisionID, second.HeadVersion)
	if err != nil {
		t.Fatal(err)
	}
	continued := edit
	continued.IdempotencyKey = "writing-session/edit-after-restore"
	continued.Bindings = []BindingSpec{firstRequest.Bindings[0], {ScopeType: ScopeHistory, ScopeID: "writing-session/round-3", Role: RoleOutput}}
	continued.BaseRevisionID, continued.ExpectedHeadVer = first.RevisionID, restored.Version
	continued.Content = []byte("# Report\n\nContinued after restore.\n\n[Source](https://example.org/evidence)\n")
	third, err := svc.CommitRevision(ctx, continued)
	if err != nil {
		t.Fatal(err)
	}
	versions, _, err := svc.ListRevisions(ctx, "writer", first.ArtifactID)
	if err != nil || len(versions) != 3 {
		t.Fatalf("versions: %d %v", len(versions), err)
	}
	for _, round := range []struct{ scopeID, revisionID string }{
		{"writing-session/round-1", first.RevisionID},
		{"writing-session/round-2", second.RevisionID},
		{"writing-session/round-3", third.RevisionID},
	} {
		binding, err := svc.FindByLegacyBinding(ctx, ScopeHistory, round.scopeID)
		if err != nil || binding.RevisionID != round.revisionID || binding.FollowHead {
			t.Fatalf("history binding: %#v %v", binding, err)
		}
	}
	head, err = svc.Head(ctx, first.ArtifactID, ChannelPublished)
	if err != nil || head.RevisionID != third.RevisionID {
		t.Fatalf("latest conversation revision: %#v %v", head, err)
	}
	for _, item := range []struct {
		id      string
		content []byte
	}{
		{first.RevisionID, firstRequest.Content}, {second.RevisionID, edit.Content}, {third.RevisionID, continued.Content},
	} {
		url, _, err := SignRevisionURL(ctx, svc, "writer", item.id)
		if err != nil {
			t.Fatal(err)
		}
		req := httptest.NewRequest(http.MethodGet, url, nil)
		req = mux.SetURLVars(req, map[string]string{"path": strings.TrimPrefix(req.URL.Path, "/static-files/")})
		response := httptest.NewRecorder()
		doc.GetSignedStaticFile(response, req)
		if response.Code != http.StatusOK || response.Body.String() != string(item.content) {
			t.Fatalf("revision download: status=%d body=%s", response.Code, response.Body.String())
		}
	}
}
