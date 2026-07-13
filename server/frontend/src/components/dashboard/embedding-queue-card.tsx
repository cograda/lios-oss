import { Database } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import type { EmbeddingQueue } from '@/lib/api'

interface EmbeddingQueueCardProps {
  data: EmbeddingQueue | { error: string } | undefined
}

export function EmbeddingQueueCard({ data }: EmbeddingQueueCardProps) {
  const hasError = data && 'error' in data
  const queue = data && !hasError ? data as EmbeddingQueue : null

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Database className="h-4 w-4" />
          Embeddings
          {queue && queue.pending > 0 && (
            <Badge variant="warning" className="ml-auto text-xs">
              {queue.pending} pending
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {!queue ? (
          <p className="text-sm text-muted-foreground">
            {hasError ? 'Unavailable' : 'Loading...'}
          </p>
        ) : (
          <div className="space-y-2">
            <div className="grid grid-cols-4 gap-2 text-center">
              {(['done', 'pending', 'processing', 'errored'] as const).map((key) => (
                <div key={key}>
                  <p className="text-lg font-semibold font-mono">{queue[key].toLocaleString()}</p>
                  <p className="text-xs text-muted-foreground capitalize">{key}</p>
                </div>
              ))}
            </div>
            {Object.keys(queue.sources).length > 0 && (
              <div className="flex gap-3 pt-1">
                {Object.entries(queue.sources).map(([source, count]) => (
                  <span key={source} className="text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">{count}</span> {source}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
