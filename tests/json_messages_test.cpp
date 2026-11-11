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
  assert(read(R"({"kind":"hello","extra":{"ignored":true}})"));
  assert(!read(R"({"kind":"tx","request_id":1,"data_hex":"00\u0000ff"})"));
  assert(!Transport::decodeHex(*command.data_hex, data, sizeof(data), size));

  assert(!read(R"({"kind":"command","request_id":1,"message":{
    "version":1,"type":"CONFIG_SET","node_id":"chair-01","boot_id":"0000000000000001",
    "seq":1,"command_id":"11111111-1111-4111-8111-111111111111","revision":1,
    "heartbeat_s":4294967295,"movement_batch_s":1,"movement_enabled":false}})"));
  SmartChair::Frame control;
  assert(command.message && SmartChair::fromJson(*command.message, control));
  assert(control.config.heartbeat_s == UINT32_MAX && !control.config.movement_enabled);
  const auto wire = SmartChair::encode(control);
  SmartChair::Frame roundtrip;
  assert(SmartChair::decode(wire, roundtrip) && roundtrip.config == control.config);

  assert(!read(R"({"kind":"event_ack","request_id":2,"message":{
    "version":1,"type":"EVENT_ACK","node_id":"chair-01","boot_id":"0000000000000001",
    "seq":2,"ack_boot_id":"0807060504030201","ack_seq":42}})"));
  assert(command.message && SmartChair::fromJson(*command.message, control));
  assert(control.ack_boot == 0x0807060504030201ULL && control.ack_seq == 42);

  // Unavailable values must be explicit null, including indefinitely open READY windows.
  SmartChair::Frame availability;
  availability.type = SmartChair::Type::READY;
  std::strcpy(availability.id.node.data(), "chair-01");
  availability.id.boot = 1;
  availability.id.seq  = 1;
  Transport::printJson(SmartChair::toJson(availability));
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
  assert(Serial.output.find("\"battery_mv\":null") != std::string::npos);
  assert(Serial.output.find("\"listen_remaining_ms\":null") != std::string::npos);
  Serial.output.clear();

  const std::string text = "quote\" slash\\ newline\n tab\t ไทย";
  Transport::printJson(Transport::ErrorMessage{.request_id = 42, .reason = text});
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
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
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
  Transport::HelloMessage hello;
  assert(!glz::read_json(hello, Serial.output));
  assert(hello.kind == "hello" && hello.role == "gateway" && hello.channel == 1);
  assert(hello.ready && hello.tx_power_dbm == 8.5 && hello.max_payload == 250);

  Serial.output.clear();
  Transport::printJson(Transport::RxMessage{.source_mac = "peer", .data_hex = "00ff"});
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
  Transport::RxMessage rx;
  assert(!glz::read_json(rx, Serial.output));
  assert(rx.kind == "rx" && rx.source_mac == "peer" && rx.data_hex == "00ff");

  Serial.output.clear();
  Transport::printJson(Transport::TxResultMessage{.request_id = 7, .mac_delivered = true});
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
  Transport::TxResultMessage result;
  assert(!glz::read_json(result, Serial.output));
  assert(result.kind == "tx_result" && result.request_id == 7 && result.mac_delivered);

  Serial.output.clear();
  Transport::printJson(Transport::SleepMessage{.wake_pin = 5, .heartbeat_ms = 900000});
  while (!Transport::usbOutput.empty())
    Transport::usbOutput.drain(8192, [](const char* p, size_t n) {
      Serial.output.append(p, n);
      return n;
    });
  Transport::SleepMessage sleep;
  assert(!glz::read_json(sleep, Serial.output));
  assert(sleep.kind == "sleep" && sleep.wake_pin == 5 && sleep.heartbeat_ms == 900000);
}
