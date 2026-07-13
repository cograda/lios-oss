interface AppLayoutProps {
  children: React.ReactNode
  bottomNav?: React.ReactNode
}

export function AppLayout({ children, bottomNav }: AppLayoutProps) {
  return (
    <div className="min-h-screen bg-background pb-20">
      <div className="max-w-lg mx-auto px-4 py-6">{children}</div>
      {bottomNav}
    </div>
  )
}
