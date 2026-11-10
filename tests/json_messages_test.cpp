#include "json_messages.h"

#include <cassert>
#include <string>

#include "transport_codec.h"

FakeSerial Serial;

struct Error {
  std::string kind;
  uint32_t request_id;
  std::string reason;
};

int main() {
  Transport::UsbCommand command;
  auto read = [&](std::string_view text) {
    command = {};
    return glz::read<Transport::UsbReadOptions{}>(command, text);
  };
  assert(!read(R"({"kind":"hello"})"));
  assert(command.kind == "hello" && !command.request_id && !command.data_hex);
  assert(!read(R"({"kind":"tx","request_id":4294967295,"data_hex":"00ff0a"})"));
  assert(command.request_id == UINT32_MAX);
  uint8_t data[Transport::MAX_PAYLOAD];
  size_t size = 0;
  assert(Transport::decodeHex(*command.data_hex, data, sizeof(data), size));
  assert(size == 3 && data[0] == 0 && data[1] == 255 && data[2] == 10);

  for (auto text : {R"([])", R"({)", R"({"kind":"hello"} trailing)",
                    R"({"kind":"tx","request_id":-1})", R"({"kind":"tx","request_id":4294967296})",
                    R"({"kind":"tx","request_id":true})", R"({"kind":"tx","request_id":1.5})",
                    R"({"kind":42})", R"({"kind":"hello","extra":[invalid]})"}) {
    assert(read(text));
  }
  assert(!read(R"({"kind":"hello","extra":{"ignored":true}})"));
  assert(!read(R"({"kind":"tx","request_id":1,"data_hex":"00\u0000ff"})"));
  assert(!Transport::decodeHex(*command.data_hex, data, sizeof(data), size));

  const std::string text = "quote\" slash\\ newline\n tab\t ไทย";
  Transport::printJson(Transport::ErrorMessage{.request_id = 42, .reason = text});
  assert(Serial.output.back() == '\n');
  assert(Serial.output.find('\n') == Serial.output.size() - 1);
  Error error;
  assert(!glz::read_json(error, Serial.output));
  assert(error.kind == "error" && error.request_id == 42 && error.reason == text);

  Serial.output.clear();
  Transport::printJson(Transport::HelloMessage{
      .role         = "gateway",
      .mac          = "44:1B:F6:8D:B7:9C",
      .peer_mac     = "48:f6:ee:12:a0:d8",
      .key_id       = "test-key",
      .channel      = 1,
      .power_save   = 0,
      .tx_power_dbm = 8.5,
      .ready        = true,
      .max_payload  = 250,
  });
  Transport::HelloMessage hello;
  assert(!glz::read_json(hello, Serial.output));
  assert(hello.kind == "hello" && hello.role == "gateway" && hello.channel == 1);
  assert(hello.ready && hello.tx_power_dbm == 8.5 && hello.max_payload == 250);

  Serial.output.clear();
  Transport::printJson(Transport::RxMessage{.source_mac = "peer", .data_hex = "00ff"});
  Transport::RxMessage rx;
  assert(!glz::read_json(rx, Serial.output));
  assert(rx.kind == "rx" && rx.source_mac == "peer" && rx.data_hex == "00ff");

  Serial.output.clear();
  Transport::printJson(Transport::TxResultMessage{.request_id = 7, .mac_delivered = true});
  Transport::TxResultMessage result;
  assert(!glz::read_json(result, Serial.output));
  assert(result.kind == "tx_result" && result.request_id == 7 && result.mac_delivered);

  Serial.output.clear();
  Transport::printJson(Transport::SleepMessage{.wake_pin = 5, .heartbeat_ms = 900000});
  Transport::SleepMessage sleep;
  assert(!glz::read_json(sleep, Serial.output));
  assert(sleep.kind == "sleep" && sleep.wake_pin == 5 && sleep.heartbeat_ms == 900000);
}
