#include <cassert>
#include <iostream>

#include "glaze/json/write.hpp"
#include "protocol_json.h"
using namespace SmartChair;

namespace {
  Header id(uint64_t boot = 0x0807060504030201ULL) {
    Header h;
    std::strcpy(h.node.data(), "chair-01");
    h.boot = boot;
    h.seq  = 1;
    return h;
  }

  Frame config(uint32_t revision, uint8_t uuid = 1) {
    Frame f;
    f.type            = Type::CONFIG_SET;
    f.id              = id(9);
    f.command[0]      = uuid;
    f.config.revision = revision;
    return f;
  }

  Sample active{0, 18000, 100, 2000, 2500, -32768, 32767};
}  // namespace

int main() {
  assert(milliseconds(UINT32_MAX) == 4294967295000ULL);
  for (auto n : {0U, 1U, UINT32_MAX}) {
    auto f               = config(1);
    f.config.heartbeat_s = f.config.movement_batch_s = n;
    assert(valid(f) == (n != 0));
    if (n) {
      Frame decoded;
      assert(decode(encode(f), decoded));
      assert(decoded.config == f.config);
    }
  }
  Frame f;
  f.id       = id();
  auto bytes = encode(f);
  assert(bytes.size() == 52 && bytes[0] == 1 && bytes[1] == 4 && bytes[2] == 20);
  for (unsigned i = 0; i < 8; ++i) assert(bytes[20 + i] == i + 1);
  Frame decoded;
  assert(decode(bytes, decoded) && decoded.id == f.id);
  for (size_t i = 0; i < bytes.size(); ++i) assert(!decode(std::span(bytes.data(), i), decoded));
  auto legacy = bytes;
  legacy.resize(44);
  legacy[2] = 12;
  assert(decode(legacy, decoded) && decoded.boot_generation == 0);
  f.boot_generation = MAX_BOOT_GENERATION;
  assert(decode(encode(f), decoded) && decoded.boot_generation == MAX_BOOT_GENERATION);
  f.boot_generation = MAX_BOOT_GENERATION + 1;
  assert(!valid(f));
  f.boot_generation = 0;
  auto bad          = bytes;
  bad[0]            = 2;
  assert(!decode(bad, decoded));
  bad    = bytes;
  bad[1] = 100;
  assert(!decode(bad, decoded));
  bad    = bytes;
  bad[3] = 1;
  assert(!decode(bad, decoded));
  bad     = bytes;
  bad[36] = 2;
  assert(!decode(bad, decoded));
  bad     = bytes;
  bad[19] = 'x';
  assert(!decode(bad, decoded));
  f.type     = Type::MOVEMENT_BATCH;
  f.duration = 65535;
  f.start    = UINT32_MAX;
  for (unsigned i = 0; i < 75; ++i) {
    auto s   = active;
    s.offset = i * 40;
    f.samples.push_back(s);
  }
  bytes = encode(f);
  assert(bytes.size() == 1108 && decode(bytes, decoded) && decoded.samples == f.samples);
  bad     = bytes;
  bad[52] = 76;
  assert(!decode(bad, decoded));
  bad     = bytes;
  bad[54] = 1;
  assert(!decode(bad, decoded));
  // Worst-case signed readings still fit the bounded JSON line.
  std::string json;
  for (size_t i = 0; i < f.samples.size(); ++i)
    f.samples[i] = {uint16_t(65535 - 74 + i), -32768, -32768, -32768, -32768, -32768, -32768};
  std::strcpy(f.id.node.data(), "abcdefghijklmno");
  f.id.boot       = UINT64_MAX;
  f.id.seq        = UINT32_MAX;
  f.common.uptime = f.common.revision = f.duration = UINT32_MAX;
  assert(valid(f));
  assert(!glz::write_json(TelemetryMessage{.source_mac = "02:00:00:00:00:01", .message = toJson(f)},
                          json));
  assert(json.size() <= Transport::MAX_LINE);
  std::cout << "maximum USB telemetry bytes: " << json.size() << '\n';
  auto j = toJson(config(UINT32_MAX));
  assert(fromJson(j, decoded));
  j.movement_enabled = false;
  assert(fromJson(j, decoded) && !decoded.config.movement_enabled);
  j.heartbeat_s.reset();
  assert(!fromJson(j, decoded));
}
