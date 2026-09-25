import assert from "node:assert/strict";
import {access, readFile} from "node:fs/promises";
import test from "node:test";

const manifest = await readFile(new URL("../shopify.app.toml", import.meta.url), "utf8");
const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);
const readme = await readFile(new URL("../README.md", import.meta.url), "utf8");

const expectedScopes = [
  "read_orders",
  "read_products",
  "read_inventory",
  "read_locations",
  "write_merchant_managed_fulfillment_orders",
];

test("declares the external TAURO application surface", () => {
  assert.match(manifest, /^application_url = "https:\/\/taurosolutions\.ar\/shopify\/install"$/m);
  assert.match(manifest, /^embedded = false$/m);
  assert.match(manifest, /^extension_directories = \[\]$/m);
  assert.match(manifest, /^web_directories = \[\]$/m);
  assert.match(
    manifest,
    /^redirect_urls = \["https:\/\/taurosolutions\.ar\/shopify\/callback"\]$/m,
  );
});

test("keeps only the scopes required by order sync, catalog reads, and fulfillment", () => {
  const match = manifest.match(/^scopes = "([^"]*)"$/m);
  assert.ok(match, "access scopes are declared");
  assert.deepEqual(match[1].split(","), expectedScopes);
  assert.doesNotMatch(match[1], /write_shipping|carrier/i);
});

test("declares all mandatory privacy webhooks", () => {
  for (const topic of [
    "customers/data_request",
    "customers/redact",
    "shop/redact",
  ]) {
    assert.match(manifest, new RegExp(`compliance_topics = \\["${topic}"\\]`));
  }
});

test("contains no embedded-template workspace", async () => {
  assert.equal(packageJson.workspaces, undefined);
  assert.equal(packageJson.dependencies, undefined);
  await assert.rejects(access(new URL("../extensions", import.meta.url)));
  await assert.rejects(access(new URL("../shared", import.meta.url)));
});

test("documents the v1 product boundary", () => {
  assert.match(readme, /externa[\s\S]*no embebida/i);
  assert.match(readme, /no cotiza en el checkout/i);
  for (const term of ["pedido", "portal", "fulfillment", "tracking"]) {
    assert.match(readme, new RegExp(term, "i"));
  }
});
