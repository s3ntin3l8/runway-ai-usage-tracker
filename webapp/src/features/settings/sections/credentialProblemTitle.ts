export function credentialProblemTitle(status: string | undefined): string | undefined {
  return status ? `A credential is ${status} — see Credentials` : undefined;
}
