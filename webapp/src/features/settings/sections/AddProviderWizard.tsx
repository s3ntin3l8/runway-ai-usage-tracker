import { claudeConnectionLabel, groupProviderFamilies, providerFamilyId } from '@/lib/providerFamilies';
// AddProviderWizard — 3-step dialog for adding a new provider account (Issue
// #287). Step 1 picks a provider (skipped when pre-scoped from
// `ProviderDetailDialog`'s "Add account" button). Step 2 collects credentials
// and debounces a /preview-account call. Step 3 confirms the saved label +
// poll interval + strategies before POSTing.
//
// Backend keeps the canonical `PUT /provider-config/{provider_id}/{account_id}`
// as the save endpoint — the wizard derives `account_id` from the preview and
// routes through that path. The endpoint is upsert (new rows create, existing
// rows update), so the wizard's 409-on-existing preview is the only guard.

import { useState } from 'react';
import { Label } from '@/components/ui/Input';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import type { ProviderConfig } from '@/api/types';
import { Step1, Step2, Step3, Stepper } from './AddProviderWizardSteps';
import type { Step2Result } from './AddProviderWizardSteps';

interface AddProviderWizardProps {
  // Pre-scope: when invoked from `ProviderDetailDialog`, the user picked a
  // provider already — skip step 1.
  preScopedProvider?: ProviderConfig | null;
  // All known providers from the registry (every entry of
  // `manager.collector_registry`, including configured + unconfigured).
  providers: ProviderConfig[];
  // All currently-known `account_id` values across the providers' accounts
  // lists — defense-in-depth against the wizard's 409-on-existing preview.
  existingAccountIdsByProvider: Map<string, Set<string>>;
  onClose: () => void;
  onSaved?: (providerId: string, accountId: string) => void;
}

export function AddProviderWizard({
  preScopedProvider,
  providers,
  existingAccountIdsByProvider,
  onClose,
  onSaved,
}: AddProviderWizardProps) {
  // Step 1 is skipped when pre-scoped: step starts at 2.
  const [step, setStep] = useState<number>(preScopedProvider ? 2 : 1);
  const [selected, setSelected] = useState<ProviderConfig | null>(preScopedProvider ?? null);
  const [step2Result, setStep2Result] = useState<Step2Result | null>(null);

  return (
    <ResponsiveDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={
        step === 1
          ? 'Add provider · step 1 of 3'
          : `Add account · ${selected?.name ?? '...'} · step ${step} of 3`
      }
      description={
        selected && step > 1
          ? 'Stored encrypted on the server.'
          : 'Choose a provider, then enter credentials. We auto-detect the account identity from the credential when possible.'
      }
      width="max-w-xl"
    >
      <div className="flex flex-col gap-4">
        <Stepper step={step} onStepClick={setStep} preScoped={!!preScopedProvider} />

        {step === 1 ? (
          <Step1
            providers={groupProviderFamilies(providers)}
            onPick={(p) => {
              setSelected(providers.find((candidate) => candidate.provider_id === p.provider_id) ?? p);
              setStep2Result(null);
              setStep(2);
            }}
          />
        ) : null}
        {/*
          Step2 and Step3 are rendered simultaneously (whenever `selected` is
          set) so local form state survives Back/Next navigation. The inactive
          step is hidden via the `hidden` attribute — visually identical to
          not rendering, but preserves useState across step transitions.
        */}
        {selected && step === 2 && providerFamilyId(selected.provider_id) === 'anthropic' && providers.some((p) => p.provider_id === 'anthropic_console') ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="claude-connection">Claude connection</Label>
            <select id="claude-connection" value={selected.provider_id}
              className="rounded-md border border-edge bg-surface-1 p-2 text-sm text-fg"
              onChange={(e) => {
                setSelected(providers.find((p) => p.provider_id === e.target.value) ?? null);
                setStep2Result(null);
              }}>
              {providers.filter((p) => providerFamilyId(p.provider_id) === 'anthropic').map((p) => (
                <option key={p.provider_id} value={p.provider_id}>{claudeConnectionLabel(p.provider_id)}</option>
              ))}
            </select>
          </div>
        ) : null}
        {selected ? (
          <>
            {/*
              Key by `selected.provider_id` so the inner form mounts a fresh
              instance when the user goes Back to step 1 and picks a
              different provider. Within a single pick, the inner state
              survives Back → Next (B3 reviewer concern).
            */}
            <div key={selected.provider_id} hidden={step !== 2}>
              <Step2
                provider={selected}
                existingAccountIds={existingAccountIdsByProvider.get(selected.provider_id) ?? new Set()}
                onBack={() => setStep(1)}
                onNext={(state) => {
                  setStep2Result(state);
                  setStep(3);
                }}
                onCancel={onClose}
                preScoped={!!preScopedProvider}
              />
            </div>
            {step2Result ? (
              <div key={`${selected.provider_id}-${step2Result.preview.suggested_account_id}`} hidden={step !== 3}>
                <Step3
                  provider={selected}
                  step2Result={step2Result}
                  onBack={() => setStep(2)}
                  onSaved={(savedAccountId) => {
                    onSaved?.(selected.provider_id, savedAccountId ?? step2Result.preview.suggested_account_id);
                    onClose();
                  }}
                />
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </ResponsiveDialog>
  );
}
