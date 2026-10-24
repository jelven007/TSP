import { cn } from '@/lib/cn'

interface Props {
  title: string
  subtitle?: React.ReactNode
  /** 标题右侧、subtitle 之前的额外节点(如状态徽标) */
  titleExtra?: React.ReactNode
  right?: React.ReactNode
  className?: string
}

export function PageHeader({ title, subtitle, titleExtra, right, className }: Props) {
  return (
    <header
      className={cn(
        'page-header min-h-16 shrink-0 border-b border-border bg-base px-5 pb-2 pt-3 max-lg:pl-16',
        'flex flex-wrap items-center justify-between gap-x-4 gap-y-2 lg:flex-nowrap',
        className,
      )}
    >
      <div className="flex min-h-[42px] min-w-0 flex-col justify-start gap-0.5 lg:max-w-sm lg:shrink-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h1 className="self-start shrink-0 whitespace-nowrap bg-gradient-to-b from-foreground via-foreground to-accent/80 bg-clip-text text-xl font-semibold tracking-normal text-transparent">
            {title}
          </h1>
          {titleExtra}
        </div>
        {subtitle && (
          <div className="truncate text-[10px] leading-tight text-muted">{subtitle}</div>
        )}
      </div>
      {right && (
        <div className="page-header-actions flex min-w-0 w-full items-center gap-2 overflow-x-auto lg:w-auto">
          {right}
        </div>
      )}
    </header>
  )
}
