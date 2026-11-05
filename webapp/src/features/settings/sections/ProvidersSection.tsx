// Provider configuration with multi-account rendering.

import { ProvidersSectionV2 } from './providersSection/ProvidersSectionV2';
import { useProviders } from './providersSection/useProviders';

export { reorderItems } from './providersSection/reorderItems';

export function ProvidersSection() {
  const { configs, layout, saveOrder } = useProviders();
  return <ProvidersSectionV2 configs={configs} layout={layout} saveOrder={saveOrder} />;
}
