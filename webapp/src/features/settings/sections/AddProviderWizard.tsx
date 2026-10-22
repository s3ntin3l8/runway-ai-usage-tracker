import { BILLING_TYPE_LABELS, BILLING_TYPE_SHORT_LABELS } from '@/lib/billingType';
import { claudeConnectionLabel, groupProviderFamilies, providerFamilyId } from '@/lib/providerFamilies';
import { ConsoleOrganizationPicker } from './ConsoleOrganizationPicker';
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

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Check, ChevronRight, Search } from 'lucide-react';
import { toast } from 'sonner';
import { ApiError } from '@/api/client';
import {
  previewAccount,
  putProviderConfig,
  type ProviderConfigUpdate,
} from '@/api/endpoints';
import type {
  AccountPreviewResponse,
  BillingType,
  CollectionStrategy,
  ProviderConfig,
} from '@/api/types';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { HelperText, Input, Label } from '@/components/ui/Input';
import { ProviderGlyph } from '@/components/ui/ProviderGlyph';
import { ResponsiveDialog } from '@/components/ui/ResponsiveDialog';
import { Switch } from '@/components/ui/Switch';
import { cn } from '@/lib/cn';
import { maskAccountId } from '@/lib/accountDisplay';
import { errorMessage } from '@/lib/errorMessage';

const PREVIEW_DEBOUNCE_MS = 300;

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

interface Step2Result {
  apiKey: string;
  sessionCookie: string;
  preview: AccountPreviewResponse;
  // How the account bills, when the operator asked the server to prove it.
  // Undefined when nothing was proven — the operator then decides.
  detectedBillingType?: BillingType;
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

function Stepper({
  step,
  onStepClick,
  preScoped,
}: {
  step: number;
  onStepClick: (n: number) => void;
  preScoped: boolean;
}) {
  const steps = preScoped
    ? [
        { n: 2, label: 'Credentials' },
        { n: 3, label: 'Confirm' },
      ]
    : [
        { n: 1, label: 'Choose' },
        { n: 2, label: 'Credentials' },
        { n: 3, label: 'Confirm' },
      ];
  return (
    <ol className="flex items-center gap-1 text-[11px] font-medium text-fg-muted">
      {steps.map((s, i) => {
        const active = step === s.n;
        const complete = step > s.n;
        return (
          <li key={s.n} className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onStepClick(s.n)}
              disabled={!complete}
              className={cn(
                'flex items-center gap-1 rounded-sm px-2 py-1',
                active
                  ? 'bg-accent-muted text-accent'
                  : complete
                    ? 'cursor-pointer text-fg hover:bg-surface-2'
                    : 'cursor-default',
              )}
            >
              <span
                className={cn(
                  'flex size-4 items-center justify-center rounded-full text-[10px]',
                  active
                    ? 'bg-accent text-accent-fg'
                    : complete
                      ? 'bg-ok-muted text-ok'
                      : 'bg-surface-2 text-fg-muted',
                )}
              >
                {complete ? <Check className="size-2.5" /> : s.n}
              </span>
              {s.label}
            </button>
            {i < steps.length - 1 ? <ChevronRight className="size-3 text-fg-subtle" /> : null}
          </li>
        );
      })}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Step 1 — pick a provider (skipped when pre-scoped)
// ---------------------------------------------------------------------------

function Step1({
  providers,
  onPick,
}: {
  providers: ProviderConfig[];
  onPick: (p: ProviderConfig) => void;
}) {
  const [search, setSearch] = useState('');
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return providers;
    return providers.filter(
      (p) => p.name.toLowerCase().includes(q) || p.provider_id.toLowerCase().includes(q),
    );
  }, [providers, search]);

  return (
    <div className="flex flex-col gap-3">
      {providers.length > 5 ? (
        <div className="relative">
          <Search
            className="pointer-events-none absolute top-1/2 left-3 size-3.5 -translate-y-1/2 text-fg-subtle"
            aria-hidden
          />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search providers…"
            aria-label="Search providers"
            className="pl-9"
          />
        </div>
      ) : null}
      {filtered.length === 0 ? (
        <Card className="py-2">
          {providers.length === 0 ? (
            <EmptyState title="No providers registered" description="Backend registry is empty." />
          ) : (
            <EmptyState
              title={`No providers match "${search}"`}
              action={
                <Button variant="ghost" size="sm" onClick={() => setSearch('')}>
                  Clear search
                </Button>
              }
            />
          )}
        </Card>
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {filtered.map((p) => (
            <button
              key={p.provider_id}
              type="button"
              onClick={() => onPick(p)}
              className="flex cursor-pointer flex-col items-start gap-1.5 rounded-md border border-edge bg-surface-1 p-3 text-left transition-colors hover:border-edge-strong hover:bg-surface-2"
            >
              <ProviderGlyph providerId={p.provider_id} name={p.name} />
              <p className="truncate text-[13px] font-medium">{p.name}</p>
              <p className="truncate text-[11px] text-fg-subtle">
                {p.account_count > 0
                  ? `${p.account_count} ${p.account_count === 1 ? 'account' : 'accounts'}`
                  : 'Not configured'}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Step 2 — credentials + debounced preview
// ---------------------------------------------------------------------------

/**
 * The billing model a 409 conflict body proved, when it proved one.
 *
 * The preview endpoint prices the credential before it checks whether the
 * account already exists, so a conflict response still carries the model the
 * server just proved. It is read out of the structured body rather than the
 * message: the message is that body's human rendering, and printing the
 * body as a string yields "[object Object]".
 */
function provenModelFromConflict(err: ApiError): BillingType | undefined {
  const detail = err.detail;
  if (typeof detail !== 'object' || detail === null) return undefined;
  const model = (detail as { suggested_billing_type?: unknown }).suggested_billing_type;
  return typeof model === 'string' && model in BILLING_TYPE_LABELS
    ? (model as BillingType)
    : undefined;
}

function Step2({
  provider,
  existingAccountIds,
  onBack,
  onNext,
  onCancel,
  preScoped,
}: {
  provider: ProviderConfig;
  existingAccountIds: Set<string>;
  onBack: () => void;
  onNext: (state: Step2Result) => void;
  onCancel: () => void;
  preScoped: boolean;
}) {
  const [apiKey, setApiKey] = useState('');
  const [cookie, setCookie] = useState('');
  const [organizationId, setOrganizationId] = useState('');
  const [preview, setPreview] = useState<AccountPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [conflict, setConflict] = useState(false);
  // How the account bills, when the operator asked the server to prove it.
  // The debounced preview never proves it — that costs a round trip to
  // Claude — so it happens once, when the button is pressed.
  const [detectedBillingType, setDetectedBillingType] = useState<BillingType | undefined>();
  const [detectError, setDetectError] = useState<string | null>(null);
  // The credential a detection was asked about. An answer — or a failure —
  // that arrives after the credential moved describes a credential Claude was
  // never asked about, and is dropped instead of shown.
  const askedRef = useRef<{ apiKey: string; cookie: string } | null>(null);
  const detectBilling = useMutation({
    mutationFn: async () => {
      const asked = { apiKey: apiKey.trim(), cookie: cookie.trim() };
      askedRef.current = asked;
      return await previewAccount({
        provider_id: provider.provider_id,
        organization_id:
          provider.provider_id === 'anthropic_console' ? organizationId : undefined,
        api_key: asked.apiKey || undefined,
        session_cookie: asked.cookie || undefined,
        detect_billing: true,
      });
    },
    onSuccess: (data) => {
      const asked = askedRef.current;
      if (!asked || asked.apiKey !== apiKey.trim() || asked.cookie !== cookie.trim()) return;
      // An inconclusive proof is not a decision: the server proved nothing,
      // so nobody chose a model and the server's own proof decides.
      setDetectedBillingType(data.suggested_billing_type ?? undefined);
      setDetectError(null);
    },
    onError: (err) => {
      const asked = askedRef.current;
      if (!asked || asked.apiKey !== apiKey.trim() || asked.cookie !== cookie.trim()) return;
      // The account already exists. The server prices the credential before
      // it checks that, so the conflict body still carries the model it
      // proved — keep it, and say what happened in words. The body is a
      // structured object: printing it would show "[object Object]".
      if (err instanceof ApiError && err.status === 409) {
        setDetectedBillingType(provenModelFromConflict(err));
        setDetectError(
          'An account with this identity already exists for this provider. Use a ' +
            'different credential, or edit the existing account.',
        );
        return;
      }
      setDetectedBillingType(undefined);
      setDetectError(errorMessage(err, 'Could not detect the billing model'));
    },
  });
  // A proof belongs to one credential. When the credential changes, the
  // proof is stale — clear it, so the wizard never saves one account's model
  // onto another, and never reports an answer about a credential Claude was
  // never asked about.
  useEffect(() => {
    setDetectedBillingType(undefined);
    setDetectError(null);
    detectBilling.reset();
  }, [apiKey, cookie]);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inflightRef = useRef<AbortController | null>(null);
  // Mirror `existingAccountIds` into a ref so the debounce effect can read
  // the latest membership without putting the Set itself in the deps array
  // (Set identity churns on every provider-configs refetch / focus, which
  // would re-fire the effect for no reason — see `existingAccountIdsKey`
  // below for the stable gate). Round 2 (Hermes): the Set was still in the
  // deps array alongside the key, defeating the gate.
  const existingAccountIdsRef = useRef(existingAccountIds);
  existingAccountIdsRef.current = existingAccountIds;
  // Credential-less providers (e.g. antigravity, which only reads a local
  // quota JSON file) can't supply an api_key or session_cookie. Skip the
  // input + debounce path entirely and use the canonical `default` identity
  // the backend already returns when no credential is passed
  // (system.py preview_account_identity — same as the post-fix default
  // sentinel). Step 2 still renders, but with no inputs, and Next becomes
  // enabled immediately.
  const credentialless =
    !provider.supports_api_key && !provider.supports_session_cookie;

  // Debounced preview. Aborts any in-flight request when a new debounce
  // fires so the LAST credential wins.
  //
  // Phase 4 (PR #295): credential-less providers (e.g. antigravity) skip
  // the credential + debounce path entirely — there's nothing to type, so
  // the preview would never settle and Next would stay disabled forever.
  // Synchronously seed the default-sentinel preview below; the conflict
  // check + Next-button state fall through from there.
  //
  // Phase 5 S2: depend on a stable per-provider key for the existing
  // accounts set (`existingAccountIdsKey`) instead of the raw `Set` — the
  // set identity changes every time `ProvidersSection`'s memo recomputes
  // (i.e. on every refetch / focus), which would re-fire this effect and
  // re-issue a preview request for no reason. Round 2 (Hermes): the Set
  // and the key were both in deps, so the churn was unchanged. Fix: read
  // the latest membership via `existingAccountIdsRef` above; the key is
  // now the sole dep that gates re-runs on membership changes.
  const existingAccountIdsKey = useMemo(
    () => [...existingAccountIds].sort().join('|'),
    [existingAccountIds],
  );
  useEffect(() => {
    if (provider.provider_id === 'anthropic_console' && !organizationId) {
      setPreview(null);
      setPreviewError(null);
      setIsPreviewing(false);
      setConflict(false);
      return;
    }
    if (credentialless) {
      // Backend returns the default sentinel when the preview endpoint is
      // called with no credential — same response we synthesize here. We
      // avoid the round-trip so the wizard is instant for the
      // no-credentials path.
      setPreview({
        suggested_account_id: 'default',
        suggested_label: null,
        label_source: 'default',
        already_exists: existingAccountIdsRef.current.has('default'),
      });
      setPreviewError(null);
      setConflict(existingAccountIdsRef.current.has('default'));
      setIsPreviewing(false);
      return;
    }
    const hint = apiKey.trim() || cookie.trim();
    if (!hint) {
      setPreview(null);
      setPreviewError(null);
      setConflict(false);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (inflightRef.current) inflightRef.current.abort();

    const ac = new AbortController();
    inflightRef.current = ac;
    setIsPreviewing(true);

    debounceRef.current = setTimeout(async () => {
      try {
        const data = await previewAccount(
          {
            provider_id: provider.provider_id,
            organization_id: provider.provider_id === 'anthropic_console' ? organizationId : undefined,
            api_key: apiKey.trim() || undefined,
            session_cookie: cookie.trim() || undefined,
          },
          ac.signal,
        );
        if (ac.signal.aborted) return;
        setPreview(data);
        // Defense-in-depth: even if the API somehow misses a collision
        // (race with another writer), our local snapshot catches it.
        const localCollision = existingAccountIdsRef.current.has(data.suggested_account_id);
        setConflict(data.already_exists || localCollision);
        setPreviewError(null);
      } catch (err) {
        if (ac.signal.aborted) return;
        // Match by HTTP status, not the error message — the API client
        // throws ApiError with the structured 409 body stringified into
        // `detail`, which yields "[object Object]" when FastAPI returns a
        // structured detail object. Status is the only reliable signal.
        if (err instanceof ApiError && err.status === 409) {
          setConflict(true);
          setPreviewError(
            'An account with this identity already exists for this provider.',
          );
        } else if (err instanceof ApiError) {
          setPreviewError(err.message);
          setConflict(false);
        } else {
          setPreviewError(errorMessage(err, 'Preview failed'));
          setConflict(false);
        }
      } finally {
        if (!ac.signal.aborted) setIsPreviewing(false);
      }
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      ac.abort();
    };
  }, [apiKey, cookie, provider.provider_id, existingAccountIdsKey, credentialless, organizationId]);

  const canProceed = !!preview && !conflict && !isPreviewing;

  return (
    <div className="flex flex-col gap-3">
      {provider.supports_api_key ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-key">{provider.api_key_label || 'API key'}</Label>
          <Input
            id="wiz-key"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="sk-…"
          />
          {provider.api_key_help ? <HelperText>{provider.api_key_help}</HelperText> : null}
        </div>
      ) : null}

      {provider.supports_session_cookie ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-cookie">{provider.session_cookie_label || 'Session cookie'}</Label>
          <Input
            id="wiz-cookie"
            type="password"
            autoComplete="off"
            value={cookie}
            onChange={(e) => { setCookie(e.target.value); setOrganizationId(''); }}
            placeholder="cookie value…"
          />
          {provider.session_cookie_help ? (
            <HelperText>{provider.session_cookie_help}</HelperText>
          ) : null}
        </div>
      ) : null}

      {provider.provider_id === 'anthropic_console' ? (
        <ConsoleOrganizationPicker cookie={cookie} value={organizationId} onSelect={setOrganizationId} />
      ) : null}

      {provider.provider_id === 'anthropic' ? (
        // The Console connection prices itself while the organization is
        // picked; a subscription has no such step, so the operator asks for
        // the proof here. What they choose in the next step still wins.
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => detectBilling.mutate()}
              loading={detectBilling.isPending}
              disabled={cookie.trim() === ''}
            >
              Detect billing model
            </Button>
            {detectedBillingType ? (
              <HelperText>Detected: {BILLING_TYPE_SHORT_LABELS[detectedBillingType]}</HelperText>
            ) : null}
            {detectBilling.isSuccess && !detectedBillingType ? (
              <HelperText>
                Could not be determined — Claude did not prove a model. Choose one in the next
                step.
              </HelperText>
            ) : null}
          </div>
          {detectError ? <HelperText>{detectError}</HelperText> : null}
          <HelperText>
            Reads how the account bills from the quota windows Claude publishes for it — a
            web session is needed. It only suggests a value; your choice in the next step
            still wins.
          </HelperText>
        </div>
      ) : null}

      {credentialless ? (
        // Providers without an api_key OR session_cookie (antigravity,
        // opencode-free, future file-only collectors) can't accept any
        // credential input — explain why nothing is here rather than
        // rendering an empty form.
        <HelperText>
          This provider has no credentials to enter — collection is
          driven by a local file. The new account will be stored under
          the default identity.
        </HelperText>
      ) : null}

      <PreviewBlock
        preview={preview}
        previewError={previewError}
        isPreviewing={isPreviewing}
        conflict={conflict}
      />

      <div className="flex justify-between">
        {preScoped ? (
          <span aria-hidden />
        ) : (
          <Button variant="ghost" onClick={onBack} type="button">
            ← Back
          </Button>
        )}
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onCancel} type="button">
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!canProceed}
            onClick={() =>
              preview
                ? onNext({ apiKey, sessionCookie: cookie, preview, detectedBillingType })
                : undefined
            }
          >
            Next →
          </Button>
        </div>
      </div>
    </div>
  );
}

function PreviewBlock({
  preview,
  previewError,
  isPreviewing,
  conflict,
}: {
  preview: AccountPreviewResponse | null;
  previewError: string | null;
  isPreviewing: boolean;
  conflict: boolean;
}) {
  if (previewError) {
    return (
      <HelperText error className="rounded-sm border border-critical/30 bg-critical/5 px-3 py-2">
        {previewError}
      </HelperText>
    );
  }
  if (!preview && isPreviewing) {
    return (
      <Card className="flex items-center justify-between gap-3 bg-surface-2 px-3 py-2">
        <span className="text-[12px] text-fg-muted">Previewing account identity…</span>
      </Card>
    );
  }
  if (!preview) {
    return (
      <HelperText>Enter a credential above to preview the account identity.</HelperText>
    );
  }
  if (conflict) {
    return (
      <HelperText
        error
        className="rounded-sm border border-critical/30 bg-critical/5 px-3 py-2"
      >
        An account with this identity already exists for this provider. Use a
        different credential or override the label below.
      </HelperText>
    );
  }
  const sourceLabel =
    preview.label_source === 'organization'
      ? 'Source: verified Console organization and billing access'
      : preview.label_source === 'email'
      ? 'Source: extracted from credential'
      : preview.label_source === 'credential_hash'
        ? 'Source: derived credential hash'
        : 'Source: fallback (no identity extractable)';
  // Never surface the raw 64-hex credential-hash id as the headline — the
  // backend intentionally withholds it as a label (`suggested_label: null`);
  // falling through to `suggested_account_id` would print the full digest.
  const headline =
    preview.suggested_label ??
    (preview.label_source === 'email'
      ? preview.suggested_account_id
      : preview.label_source === 'credential_hash'
        ? 'No identity in credential'
        : 'Default account');
  const badgeLabel =
    preview.label_source === 'credential_hash' ? 'hash' : preview.label_source;
  return (
    <Card className="flex flex-col gap-1.5 bg-surface-2 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] font-medium">{headline}</span>
        <Badge variant="accent">{badgeLabel}</Badge>
      </div>
      <p className="text-[11px] text-fg-subtle">{sourceLabel}</p>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Step 3 — confirm + save
// ---------------------------------------------------------------------------

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$/;

function Step3({
  provider,
  step2Result,
  onBack,
  onSaved,
}: {
  provider: ProviderConfig;
  step2Result: Step2Result;
  onBack: () => void;
  onSaved: (savedAccountId?: string) => void;
}) {
  const queryClient = useQueryClient();
  const accountId = step2Result.preview.suggested_account_id;
  // What the operator typed, kept apart from what the server suggested.
  // `undefined` = they typed nothing, so the suggestion stands. The split
  // matters because the suggestion can change under us — the operator goes
  // back, changes the credential, or asks for another preview — and a
  // mount-time latch would keep answering for the account that was there
  // before.
  const [labelOverride, setLabelOverride] = useState<string | undefined>();
  const label = labelOverride ?? step2Result.preview.suggested_label ?? '';
  const [workspaceId, setWorkspaceId] = useState('');
  const [pollInterval, setPollInterval] = useState('');
  // How the account bills. `undefined` means "nobody chose" — the operator
  // neither picked a model nor asked for a proof — and then the field is not
  // sent, so the server's own proof decides. A choice (the operator's, or a
  // proof they asked for) is sent, and it always wins.
  //
  // The proof is read from the current step-2 result on every render rather
  // than latched when this component mounted: the operator can go back and
  // ask for the proof *after* reaching this step, and a latch would drop it
  // — saving the account unpriced, which is the symptom this whole flow
  // exists to close.
  const [chosenBillingType, setChosenBillingType] = useState<BillingType | undefined>();
  const billingType = chosenBillingType ?? step2Result.detectedBillingType;
  const shownBillingType =
    billingType ?? step2Result.preview.suggested_billing_type ?? 'unknown';
  const [strategies, setStrategies] = useState<{ id: string; enabled: boolean }[]>(() =>
    initStrategies(provider),
  );

  const effectiveAccountId =
    step2Result.preview.label_source === 'credential_hash' && EMAIL_RE.test(label.trim())
      ? label.trim().toLowerCase()
      : accountId;
  // Only the Claude family bills by a model Runway knows how to present.
  const isClaudeFamily = providerFamilyId(provider.provider_id) === 'anthropic';
  const maskedEffectiveAccountId = maskAccountId(effectiveAccountId);

  const save = useMutation({
    mutationFn: () => {
      const body: ProviderConfigUpdate = {
        enabled: true,
        ...(isClaudeFamily && billingType ? { billing_type: billingType } : {}),
        // Empty string means "clear" server-side (mirrors the provider account form's
        // behaviour); trimmed value otherwise.
        account_label: label.trim(),
        poll_interval_seconds: pollInterval.trim() === '' ? null : Number(pollInterval),
        collection_strategies: strategies.map(({ id, enabled: e }) => ({ id, enabled: e })),
      };
      if (provider.provider_id === 'opencode') body.opencode_workspace_id = workspaceId.trim();
      // Send the credentials the user typed in step 2 if non-empty — the
      // endpoint treats absence as "keep existing" and non-empty as "set".
      // Empty-string-vs-absent is distinguished server-side: an empty
      // string would mean "clear", so we omit the field entirely when
      // blank so existing credentials aren't wiped on first save.
      if (step2Result.apiKey.trim() !== '') body.api_key = step2Result.apiKey.trim();
      if (step2Result.sessionCookie.trim() !== '') {
        body.session_cookie = step2Result.sessionCookie.trim();
      }
      return putProviderConfig(provider.provider_id, effectiveAccountId, body);
    },
    onSuccess: (data) => {
      toast.success(`${provider.name} · ${label.trim() || maskedEffectiveAccountId} saved`);
      queryClient.invalidateQueries({ queryKey: ['system', 'provider-configs'] });
      queryClient.invalidateQueries({ queryKey: ['usage'] });
      onSaved(data?.account_id ?? effectiveAccountId);
    },
    onError: (err) => toast.error(err.message),
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-label">Account label</Label>
          <Input
            id="wiz-label"
            value={label}
            onChange={(e) => setLabelOverride(e.target.value)}
            placeholder={accountId === 'default' ? 'Default account' : maskAccountId(accountId)}
          />
          <HelperText>
            Saved under account_id={maskedEffectiveAccountId || 'default'}
            {maskedEffectiveAccountId && maskedEffectiveAccountId !== effectiveAccountId
              ? ' (masked)'
              : ''}
          </HelperText>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-poll">Poll interval (s)</Label>
          <Input
            id="wiz-poll"
            type="number"
            inputMode="numeric"
            min={30}
            value={pollInterval}
            onChange={(e) => setPollInterval(e.target.value)}
            placeholder={`default ${provider.effective_poll_interval ?? ''}`}
          />
        </div>
      </div>

      {isClaudeFamily ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-billing-type">Billing model</Label>
          <select
            id="wiz-billing-type"
            value={shownBillingType}
            onChange={(e) => setChosenBillingType(e.target.value as BillingType)}
            className="h-9 rounded-md border border-border bg-surface-1 px-3 text-sm text-fg"
          >
            {(Object.keys(BILLING_TYPE_LABELS) as BillingType[]).map((type) => (
              <option key={type} value={type}>{BILLING_TYPE_LABELS[type]}</option>
            ))}
          </select>
          <HelperText>
            How the account is billed. Runway suggests it from the cards the account
            publishes; your choice is what gets saved.
          </HelperText>
        </div>
      ) : null}

      {provider.provider_id === 'opencode' ? (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="wiz-opencode-workspace-id">OpenCode workspace ID</Label>
          <Input
            id="wiz-opencode-workspace-id"
            autoComplete="off"
            value={workspaceId}
            onChange={(e) => setWorkspaceId(e.target.value)}
            placeholder="Required when this account has multiple Go workspaces"
          />
          <HelperText>Find the ID in your OpenCode Console workspace settings.</HelperText>
        </div>
      ) : null}

      {strategies.length > 0 ? (
        <fieldset className="flex flex-col gap-2 rounded-sm border border-edge p-3">
          <legend className="px-1 text-xs font-medium text-fg-muted">Collection strategies</legend>
          {strategies.map((s) => (
            <div key={s.id} className="flex items-center justify-between">
              <span className="text-[13px]">{s.id}</span>
              <Switch
                checked={s.enabled}
                onCheckedChange={(enabled) =>
                  setStrategies((prev) => prev.map((x) => (x.id === s.id ? { ...x, enabled } : x)))
                }
                aria-label={s.id}
              />
            </div>
          ))}
        </fieldset>
      ) : null}

      <div className="flex justify-between">
        <Button variant="ghost" onClick={onBack} type="button">
          ← Back
        </Button>
        <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </div>
    </div>
  );
}

function initStrategies(provider: ProviderConfig): { id: string; enabled: boolean }[] {
  // 2nd+ account → copy from the provider's first existing account's
  // user-customized strategies. 1st account → fall back to registry
  // defaults. Works for both the pre-scoped flow (Add account inside
  // ProviderDetailDialog) and the empty-canvas flow (Add provider →
  // pick an already-configured provider from step 1).
  const first = provider.accounts[0];
  if (first?.collection_strategies && first.collection_strategies.length > 0) {
    return first.collection_strategies.map((s: CollectionStrategy) => ({
      id: s.id,
      enabled: s.enabled,
    }));
  }
  return (provider.supported_strategies ?? []).map((s) => ({
    id: s.id,
    enabled: s.enabled,
  }));
}
