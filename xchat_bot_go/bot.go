// Simple X Chat echo bot in Go.
//
// Unlocks keys via Juicebox, connects to the activity stream, decrypts
// incoming text messages, and echoes them back.
//
// Configuration (all read from .env in the working directory):
//
//	BEARER_TOKEN          — App bearer token for the activity stream
//	OAUTH_ACCESS_TOKEN    — User OAuth2 access token (from the login flow)
//	XCHAT_PIN             — PIN for Juicebox key recovery (default: prompted)
//	XCHAT_SEND_BASE_URL   — API base URL (default: https://api.x.com)
//	XCHAT_STREAM_BASE_URL — Stream base URL (default: https://api.x.com)
//
// Prerequisites:
//  1. Build the Rust static lib:  cd ../chat-xdk && make go-lib
//  2. Fill in .env with your credentials
//  3. Run:  CGO_ENABLED=1 go run bot.go
package main

import (
	"bufio"
	"bytes"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/xdevplatform/chat-xdk/go/chatxdk"
)

// ---------------------------------------------------------------------------
// Config helpers
// ---------------------------------------------------------------------------

func loadEnv() map[string]string {
	env := map[string]string{}
	// Look for .env in current dir, then parent (repo root)
	f, err := os.Open(".env")
	if err != nil {
		f, err = os.Open("../.env")
	}
	if err != nil {
		return env
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		k, v, _ := strings.Cut(line, "=")
		env[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"'`)
	}
	// Environment variables override .env
	for _, kv := range os.Environ() {
		k, v, _ := strings.Cut(kv, "=")
		if _, exists := env[k]; exists {
			env[k] = v
		}
	}
	return env
}

func envOr(env map[string]string, key, fallback string) string {
	if v := env[key]; v != "" {
		return v
	}
	return fallback
}

func requireEnv(env map[string]string, key string) string {
	v := env[key]
	if v == "" {
		log.Fatalf("Missing required config: %s (set in .env or environment)", key)
	}
	return v
}

// ---------------------------------------------------------------------------
// Activity stream (newline-delimited JSON over HTTP/1.1 chunked)
// ---------------------------------------------------------------------------

type activityEvent struct {
	Data struct {
		EventType string                 `json:"event_type"`
		Payload   map[string]interface{} `json:"payload"`
	} `json:"data"`
}

func streamActivity(streamURL, bearerToken string, events chan<- activityEvent) {
	req, _ := http.NewRequest("GET", streamURL+"/2/activity/stream", nil)
	req.Header.Set("Authorization", "Bearer "+bearerToken)

	// Force HTTP/1.1 — the activity stream uses chunked transfer encoding
	// which Go's HTTP/2 client may buffer differently.
	transport := &http.Transport{
		TLSNextProto:       make(map[string]func(string, *tls.Conn) http.RoundTripper),
		DisableCompression: true,
		DialContext: (&net.Dialer{
			Timeout:   30 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
	}
	client := &http.Client{Timeout: 0, Transport: transport}
	resp, err := client.Do(req)
	if err != nil {
		log.Fatalf("stream connect failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		body, _ := io.ReadAll(resp.Body)
		log.Fatalf("stream HTTP %d: %s", resp.StatusCode, string(body))
	}
	log.Println("Connected to activity stream")

	// Read newline-delimited JSON (same as Python SDK iter_content).
	buf := make([]byte, 0, 1<<20)
	tmp := make([]byte, 4096)
	for {
		n, readErr := resp.Body.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			for {
				idx := bytes.IndexByte(buf, '\n')
				if idx < 0 {
					break
				}
				line := strings.TrimSpace(string(buf[:idx]))
				buf = buf[idx+1:]
				if line == "" || line == "{}" {
					continue
				}
				var evt activityEvent
				if json.Unmarshal([]byte(line), &evt) == nil {
					events <- evt
				}
			}
		}
		if readErr != nil {
			if readErr != io.EOF {
				log.Printf("stream read error: %v", readErr)
			}
			break
		}
	}
}

// ---------------------------------------------------------------------------
// Send a reply via X Chat API
// ---------------------------------------------------------------------------

func sendReply(sendURL, accessToken, convID, convToken string, body map[string]interface{}) {
	apiConvID := strings.ReplaceAll(convID, ":", "-")
	url := fmt.Sprintf("%s/2/chat/conversations/%s/messages", sendURL, apiConvID)

	if convToken != "" {
		body["conversation_token"] = convToken
	}
	jsonBody, _ := json.Marshal(body)

	req, _ := http.NewRequest("POST", url, bytes.NewReader(jsonBody))
	req.Header.Set("Authorization", "Bearer "+accessToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Printf("send failed: %v", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		respBody, _ := io.ReadAll(resp.Body)
		log.Printf("send HTTP %d: %s", resp.StatusCode, string(respBody))
	}
}

// ---------------------------------------------------------------------------
// Key extraction from KeyChange events
// ---------------------------------------------------------------------------

// pickDecryptableKey decrypts a KeyChange event and finds the participant key
// that belongs to this bot. Returns (encrypted_key, key_version).
func pickDecryptableKey(chat *chatxdk.Chat, keyChangeEventB64 string) (string, string) {
	event, err := chat.DecryptEvent(keyChangeEventB64, "", nil)
	if err != nil {
		return "", ""
	}
	if event.Type != "KeyChange" {
		return "", ""
	}
	kc := event.AsKeyChange()
	if kc == nil {
		return "", ""
	}
	for _, pk := range kc.ParticipantKeys {
		if pk.EncryptedKey == "" {
			continue
		}
		if _, err := chat.DecryptConversationKey(pk.EncryptedKey); err == nil {
			return pk.EncryptedKey, kc.KeyVersion
		}
	}
	return "", kc.KeyVersion
}

// ---------------------------------------------------------------------------
// Unlock: GET /2/users/me, GET /2/users/:id/public_keys, chat.Unlock(pin)
// ---------------------------------------------------------------------------

func fetchUserID(apiBase, accessToken string) string {
	req, _ := http.NewRequest("GET", apiBase+"/2/users/me", nil)
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatalf("GET /2/users/me failed: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		log.Fatalf("GET /2/users/me HTTP %d: %s", resp.StatusCode, string(body))
	}
	var result map[string]interface{}
	json.Unmarshal(body, &result)
	data, _ := result["data"].(map[string]interface{})
	id, _ := data["id"].(string)
	return id
}

func fetchPublicKeys(apiBase, accessToken, userID string) (juiceboxConfig, signingKeyVersion string) {
	url := fmt.Sprintf("%s/2/users/%s/public_keys?public_key.fields=version,public_key,signing_public_key,juicebox_config", apiBase, userID)
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		log.Fatalf("GET public_keys failed: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		log.Fatalf("GET public_keys HTTP %d: %s", resp.StatusCode, string(body))
	}

	var result map[string]interface{}
	json.Unmarshal(body, &result)

	// data can be a single object or an array — use the last (most recent) entry
	var data map[string]interface{}
	switch d := result["data"].(type) {
	case map[string]interface{}:
		data = d
	case []interface{}:
		if len(d) == 0 {
			log.Fatal("Empty data array in public_keys response")
		}
		data, _ = d[len(d)-1].(map[string]interface{})
	default:
		log.Fatalf("Unexpected data type in public_keys response: %T", result["data"])
	}
	signingKeyVersion = fmt.Sprintf("%v", data["version"])

	jbRaw, ok := data["juicebox_config"].(map[string]interface{})
	if !ok {
		log.Fatal("Missing juicebox_config in public_keys response")
	}

	// Normalize juicebox_config (same logic as Python unlock.py)
	tokenMap, _ := jbRaw["token_map"].([]interface{})
	keyStoreJSON, _ := jbRaw["key_store_token_map_json"].(string)
	maxGuessCount := 20.0
	if mgc, ok := jbRaw["max_guess_count"].(float64); ok {
		maxGuessCount = mgc
	}
	tokens := map[string]string{}
	for _, entry := range tokenMap {
		e, _ := entry.(map[string]interface{})
		key, _ := e["key"].(string)
		val, _ := e["value"].(map[string]interface{})
		token, _ := val["token"].(string)
		if key != "" && token != "" {
			tokens[key] = token
		}
	}
	if keyStoreJSON != "" && len(tokens) > 0 {
		b, _ := json.Marshal(map[string]interface{}{
			"sdk_config":      keyStoreJSON,
			"tokens":          tokens,
			"max_guess_count": int(maxGuessCount),
		})
		juiceboxConfig = string(b)
	} else {
		b, _ := json.Marshal(jbRaw)
		juiceboxConfig = string(b)
	}
	return juiceboxConfig, signingKeyVersion
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

func main() {
	env := loadEnv()

	accessToken := requireEnv(env, "OAUTH_ACCESS_TOKEN")
	bearerToken := requireEnv(env, "BEARER_TOKEN")
	pin := envOr(env, "XCHAT_PIN", "")
	if pin == "" {
		log.Fatal("Missing XCHAT_PIN in .env")
	}
	apiBase := envOr(env, "XCHAT_SEND_BASE_URL", "https://api.x.com")
	streamURL := envOr(env, "XCHAT_STREAM_BASE_URL", "https://api.x.com")

	// Step 1: Resolve user ID from OAuth token
	userID := fetchUserID(apiBase, accessToken)
	if userID == "" {
		log.Fatal("Could not resolve user ID from /2/users/me")
	}
	log.Printf("Resolved user_id=%s", userID)

	// Step 2: Fetch Juicebox config
	juiceboxConfig, signingKeyVersion := fetchPublicKeys(apiBase, accessToken, userID)

	// Step 3: Unlock keys with PIN via Juicebox
	chat := chatxdk.New()
	defer chat.Close()
	if err := chat.Unlock(pin, juiceboxConfig); err != nil {
		log.Fatalf("Unlock failed: %v", err)
	}
	log.Println("Keys unlocked via Juicebox")

	// Step 4: Connect to activity stream and echo messages
	encKeyCache := map[string]string{}
	events := make(chan activityEvent, 64)
	go streamActivity(streamURL, bearerToken, events)

	log.Println("Listening for messages...")
	for evt := range events {
		if evt.Data.EventType != "chat.received" {
			continue
		}
		p := evt.Data.Payload
		convID, _ := p["conversation_id"].(string)
		encodedEvent, _ := p["encoded_event"].(string)
		if convID == "" || encodedEvent == "" {
			continue
		}

		// Skip messages sent by the bot itself
		if senderID, _ := p["sender_id"].(string); senderID == userID {
			continue
		}

		// Resolve conversation key (direct, key-change event, or cache)
		encKey, _ := p["encrypted_conversation_key"].(string)
		keyVersion, _ := p["conversation_key_version"].(string)
		if encKey == "" {
			if kcB64, _ := p["conversation_key_change_event"].(string); kcB64 != "" {
				encKey, keyVersion = pickDecryptableKey(chat, kcB64)
			}
		}
		if encKey == "" {
			encKey = encKeyCache[convID]
		}
		if encKey != "" {
			encKeyCache[convID] = encKey
		}
		if encKey == "" || keyVersion == "" {
			continue
		}

		// Decrypt
		event, err := chat.DecryptEvent(encodedEvent, encKey, nil)
		if err != nil || event.Type != "Message" {
			continue
		}
		msg := event.AsMessage()
		if msg == nil || msg.Content.ContentType != "Text" || msg.Content.TextContent == nil {
			continue
		}
		text := msg.Content.TextContent.Text
		log.Printf("Received: %q", text)

		// Echo reply
		reply := fmt.Sprintf("got it: %s", text)
		msgID := uuid.New().String()
		payload, err := chat.EncryptMessageForAPI(chatxdk.EncryptMessageForAPIParams{
			MessageID:              msgID,
			SenderID:               userID,
			ConversationID:         convID,
			ConversationKeyB64:     encKey,
			Text:                   reply,
			ConversationKeyVersion: keyVersion,
			SigningKeyVersion:      signingKeyVersion,
		})
		if err != nil {
			log.Printf("encrypt failed: %v", err)
			continue
		}

		convToken, _ := p["conversation_token"].(string)
		sendReply(apiBase, accessToken, convID, convToken, map[string]interface{}{
			"message_id":                      msgID,
			"encoded_message_create_event":    payload.EncryptedContent,
			"encoded_message_event_signature": payload.EncodedEventSignature,
		})
		log.Printf("Replied: %q", reply)
	}
}
