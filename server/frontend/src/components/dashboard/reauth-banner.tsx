import { KeyRound, ExternalLink } from 'lucide-react'
import type { ReauthNeeded } from '@/lib/api'

interface ReauthBannerProps {
  items: ReauthNeeded[]
}

function shortReason(reason: string | null): string {
  if (!reason) return 'token revoked'
  if (reason.includes('invalid_grant')) return 'refresh token revoked or expired'
  if (reason.includes('unauthorized_client')) return 'OAuth client disabled'
  if (reason.includes('invalid_client')) return 'OAuth client mismatch'
  return reason.length > 80 ? reason.slice(0, 77) + '…' : reason
}

export function ReauthBanner({ items }: ReauthBannerProps) {
  if (items.length === 0) return null

  return (
    <div className="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3">
      <div className="flex items-start gap-3">
        <KeyRound className="h-4 w-4 text-warning shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium">
            {items.length === 1
              ? `1 account needs re-authentication`
              : `${items.length} accounts need re-authentication`}
          </p>
          <ul className="mt-2 space-y-1.5">
            {items.map((item) => (
              <li key={`${item.provider}:${item.account_email}`} className="flex items-center gap-2 text-sm">
                <span className="font-mono text-xs text-muted-foreground">{item.provider}</span>
                <span className="truncate">{item.account_email}</span>
                <span className="text-xs text-muted-foreground truncate">— {shortReason(item.reason)}</span>
                <a
                  href={item.reauth_url}
                  className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-warning hover:underline shrink-0"
                >
                  Re-authenticate
                  <ExternalLink className="h-3 w-3" />
                </a>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
