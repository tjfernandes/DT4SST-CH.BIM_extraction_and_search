import React, { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Send, Bot, User, Loader2, Database, Trash2 } from 'lucide-react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';



// Utility for tailwind classes merging
function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  plan?: any;
}

const API_URL = 'http://localhost:8000/chat';

export default function App() {
 
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content: 'Olá! Sou o seu assistente HBIM. Como posso ajudar a explorar os seus modelos hoje?',
    },
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isLoading]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage = input.trim();
    console.log('Enviando mensagem:', userMessage); // <-- ADICIONAR ESTE
    setInput('');
    setMessages((prev) => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);

    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          message: userMessage,
          history: messages.slice(-10).map((m) => ({ role: m.role, content: m.content })),
        }),
      });
      
      if (!response.ok) {
        throw new Error('Falha na comunicação com o servidor');
      }

      const data = await response.json();
      console.log("=== BACKEND ===");
      console.log(data);
      console.log("PLAN:", data.plan);
      console.log("CONDITIONS:", data?.plan?.conditions);

      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: data.response, plan: data.plan },
      ]);
    } catch (error) {
      console.error('Error:', error);
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: 'Desculpe, ocorreu um erro ao processar o seu pedido.' },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  const clearChat = () => {
    setMessages([
      {
        role: 'assistant',
        content: 'Olá! Sou o seu assistente HBIM. Como posso ajudar a explorar os seus modelos hoje?',
      },
    ]);
  };

  return (
    <div className="flex flex-col h-screen bg-slate-50 text-slate-900 font-sans">
      {/* Header */}
      <header className="flex items-center justify-between px-6 py-4 bg-white border-b border-slate-200 shadow-sm">
        <div className="flex items-center gap-2">
          <div className="bg-blue-600 p-1.5 rounded-lg">
            <Database className="w-5 h-5 text-white" />
          </div>
          <h1 className="text-xl font-bold tracking-tight text-slate-800">HBIM Search</h1>
        </div>
        <button
          onClick={clearChat}
          className="p-2 text-slate-400 hover:text-red-500 transition-colors rounded-full hover:bg-slate-100"
          title="Limpar conversa"
        >
          <Trash2 className="w-5 h-5" />
        </button>
      </header>

      {/* Main Chat Area */}
      <main className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6 max-w-4xl mx-auto w-full">
        {messages.map((message, index) => (
          <div
            key={index}
            className={cn(
              "flex w-full gap-4 animate-in fade-in slide-in-from-bottom-2 duration-300",
              message.role === 'user' ? "justify-end" : "justify-start"
            )}
          >
            {message.role === 'assistant' && (
              <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center shrink-0 border border-blue-200">
                <Bot className="w-5 h-5 text-blue-600" />
              </div>
            )}
            
            <div
              className={cn(
                "max-w-[85%] rounded-2xl px-4 py-3 shadow-sm",
                message.role === 'user'
                  ? "bg-blue-600 text-white rounded-tr-none"
                  : "bg-white text-slate-800 border border-slate-200 rounded-tl-none"
              )}
            >
              {message.role === 'assistant' ? (
                <div className="prose prose-slate max-w-none prose-p:leading-relaxed prose-pre:bg-slate-900 prose-pre:text-slate-50 prose-code:text-blue-600 prose-code:bg-blue-50 prose-code:px-1 prose-code:rounded">
                  <ReactMarkdown>{message.content}</ReactMarkdown>
                  
                  {message.plan && message.plan.needs_rag && (
                    <div className="mt-3 pt-3 border-t border-slate-100">
                      <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                        <Database className="w-3 h-3" />
                        <span>Pesquisa BIM Executada</span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2">
                        {message.plan.ifc_class && (
                          <span className="bg-slate-100 text-slate-600 px-2 py-0.5 rounded text-xs font-medium">
                            {message.plan.ifc_class}
                          </span>
                        )}
                        {message.plan.conditions?.map((c: any, i: number) => {
                          const opMap: Record<string, string> = {
                            gte: '≥',
                            gt: '>',
                            lte: '≤',
                            lt: '<',
                            eq: '=',
                            approx: '≈',
                            contains: '⊃'
                          };
                          const opDisplay = opMap[c.op] || c.op;
                          return (
                            <span key={i} className="bg-blue-50 text-blue-600 px-2 py-0.5 rounded text-xs font-medium border border-blue-100">
                              {c.field} {opDisplay} {Array.isArray(c.value) ? c.value.join(', ') : c.value}
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="leading-relaxed">{message.content}</p>
              )}
            </div>

            {message.role === 'user' && (
              <div className="w-8 h-8 rounded-full bg-slate-200 flex items-center justify-center shrink-0 border border-slate-300">
                <User className="w-5 h-5 text-slate-600" />
              </div>
            )}
          </div>
        ))}
        
        {isLoading && (
          <div className="flex justify-start gap-4">
            <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center border border-blue-200">
              <Bot className="w-5 h-5 text-blue-600" />
            </div>
            <div className="bg-white border border-slate-200 rounded-2xl rounded-tl-none px-4 py-3 shadow-sm flex items-center gap-3">
              <Loader2 className="w-5 h-5 text-blue-500 animate-spin" />
              <span className="text-slate-500 text-sm font-medium">A pensar...</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </main>

      {/* Input Area */}
      <footer className="p-4 md:p-6 bg-white border-t border-slate-200 shadow-[0_-1px_3px_0_rgba(0,0,0,0.05)]">
        <form 
          onSubmit={handleSubmit}
          className="max-w-4xl mx-auto relative group"
        >
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Pergunte algo sobre o modelo (ex: 'quantas portas existem?')"
            disabled={isLoading}
            className="w-full pl-4 pr-12 py-3.5 bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all disabled:opacity-50 text-slate-800 placeholder:text-slate-400"
          />
          <button
            type="submit"
            disabled={!input.trim() || isLoading}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed transition-all shadow-sm"
          >
            {isLoading ? (
              <Loader2 className="w-5 h-5 animate-spin" />
            ) : (
              <Send className="w-5 h-5" />
            )}
          </button>
        </form>
        <p className="max-w-4xl mx-auto text-center mt-3 text-xs text-slate-400">
          Pesquisa de Dados BIM inteligente &bull; Desenvolvido com FastAPI e React
        </p>
      </footer>
    </div>
  );
}
