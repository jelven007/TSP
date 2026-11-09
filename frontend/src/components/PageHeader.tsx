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
        'page-header h-[104px] shrink-0 border-b border-border bg-base px-5 pb-[9px] pt-3 lg:h-16 lg:pb-[11px]',
        'grid min-w-0 grid-rows-[42px_32px] items-center gap-x-4 gap-y-2 lg:grid-cols-[auto_minmax(0,1fr)] lg:grid-rows-[40px] lg:gap-y-0',
        className,
      )}
    >
      <div className="flex min-w-0 self-start flex-col justify-start gap-0.5 max-lg:pl-11 lg:max-w-sm">
        <div className="flex min-w-0 items-center gap-2 overflow-x-auto">
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
        <div className="page-header-actions flex min-w-0 w-full items-center gap-2 overflow-x-auto lg:justify-self-end lg:w-auto lg:max-w-full">
          {right}
        </div>
      )}
    </header>
  )
}
