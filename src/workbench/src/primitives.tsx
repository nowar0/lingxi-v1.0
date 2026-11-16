import React from 'react';
const Icon=({size=16,className='',children}:any)=><svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
export const IconCloseOutlineRegular=(p:any)=><Icon {...p}><path d="m6 6 12 12M18 6 6 18"/></Icon>;
export const IconBranchOutlineRegular=(p:any)=><Icon {...p}><circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 6v12m0-5c8 0 12-3 12-6"/></Icon>;
export const IconChevronDownOutlineMedium=(p:any)=><Icon {...p}><path d="m6 9 6 6 6-6"/></Icon>;
