/** Keep voice requests bounded while preserving complete Unicode characters. */
export function sentenceParts(text:string){
 const clean=text.replace(/```[\s\S]*?```/g,' 代码内容已略过。 ').replace(/!\[[^\]]*\]\([^)]*\)/g,'').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*#`>]/g,'').trim()
 const chars=Array.from(clean),parts:string[]=[];let current=''
 chars.forEach((char,index)=>{current+=char;const sentence=/[。！？!?；;\n]/.test(char)||(char==='.'&&(!chars[index+1]||/\s/.test(chars[index+1])));if(sentence||current.length>=160){if(current.trim())parts.push(current.trim());current=''}})
 if(current.trim())parts.push(current.trim());return parts
}
