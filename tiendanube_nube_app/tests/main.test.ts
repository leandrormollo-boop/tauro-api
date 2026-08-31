import { describe, expect, it, vi } from "vitest";

import {
  App,
  buildTauroLabels,
  TAURO_NACIONAL_CODE,
} from "../src/main";

describe("TAURO Nacional NubeSDK", () => {
  it("etiqueta solamente la opcion perteneciente a TAURO", () => {
    const labels = buildTauroLabels([
      { id: "tauro-1", code: TAURO_NACIONAL_CODE },
      { id: "otro-1", code: "otro" },
    ] as never);

    expect(labels).toEqual({
      "tauro-1": {
        title: "TAURO Nacional",
        description: "Entrega a domicilio con seguimiento de punta a punta.",
      },
    });
  });

  it("publica la etiqueta una sola vez aunque Tiendanube repita el evento", () => {
    const shipping = {
      options: [{ id: "tauro-1", code: TAURO_NACIONAL_CODE }],
    };
    const listeners = new Map<string, () => void>();
    const send = vi.fn();
    const nube = {
      getState: () => ({ shipping }),
      send,
      on: (event: string, listener: () => void) => listeners.set(event, listener),
    };

    App(nube as never);
    listeners.get("shipping:update")?.();

    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("shipping:update:label", expect.any(Function));
  });
});
