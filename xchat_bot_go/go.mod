module xchat-bot-go

go 1.21

require (
	github.com/google/uuid v1.6.0
	github.com/xdevplatform/chat-xdk/go/chatxdk v0.0.0
)

replace github.com/xdevplatform/chat-xdk/go/chatxdk => ../../chat-xdk/crates/go/go/chatxdk
