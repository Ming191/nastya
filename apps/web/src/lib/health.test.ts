import assert from "node:assert/strict";
import test from "node:test";

import { getHealthStatus } from "./health";

test("bootstrap web health reports that AI is not yet integrated", () => {
  assert.deepEqual(getHealthStatus(), {
    service: "web",
    status: "ok",
    aiReady: false,
  });
});
