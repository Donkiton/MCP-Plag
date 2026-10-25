import { expect, test } from "bun:test";
import { templatePath } from "../telemetry/route-template.js";

test("all account connection capabilities are redacted regardless of token alphabet", () => {
  for (const token of ["aaaaaaaaaaaaaaaaaaaa", "QrToken_with-underscore", "1234567890abcdef1234567890"]) {
    expect(templatePath(`/accounts/add/${token}`)).toBe("/accounts/add/:token");
    expect(templatePath(`/accounts/add/${token}/qr`)).toBe("/accounts/add/:token/qr");
  }
});
