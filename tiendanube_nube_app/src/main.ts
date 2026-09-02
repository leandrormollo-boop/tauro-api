import type { NubeSDK, ShippingOption } from "@tiendanube/nube-sdk-types";

export const TAURO_NACIONAL_CODE = "tauro_nacional_domicilio";

type Label = {
  title: string;
  description: string;
};

export function buildTauroLabels(
  options: readonly ShippingOption[] | undefined,
): Record<string, Label> {
  const labels: Record<string, Label> = {};
  for (const option of options ?? []) {
    if (option.code !== TAURO_NACIONAL_CODE) continue;
    labels[option.id] = {
      title: "TAURO Solutions Ar",
      description: "Entrega a domicilio con seguimiento de punta a punta.",
    };
  }
  return labels;
}

export function App(nube: NubeSDK) {
  let lastSignature = "";

  const syncLabels = () => {
    const shipping = nube.getState().shipping;
    if (!shipping) return;

    const customLabels = buildTauroLabels(shipping.options);
    const signature = JSON.stringify(customLabels);
    if (signature === "{}" || signature === lastSignature) return;
    lastSignature = signature;

    nube.send("shipping:update:label", () => ({
      shipping: {
        selected: shipping.selected,
        options: shipping.options ?? [],
        custom_labels: customLabels,
      },
    }));
  };

  syncLabels();
  nube.on("shipping:update", syncLabels);
}
