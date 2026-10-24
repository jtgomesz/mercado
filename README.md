# Mercado Pro

Painel local de gestão para mercado, com interface responsiva e banco SQLite persistente. O projeto usa **Python 3.10+ e bibliotecas padrão**, sem instalar Flask, sem CDN e sem depender da internet para exibir a interface.

## Iniciar no Windows

1. Extraia o ZIP para uma pasta normal, por exemplo `Documentos\\MercadoPro`.
2. Instale o Python 3.10 ou mais recente em [python.org](https://www.python.org/downloads/windows/) caso ainda não esteja instalado. Na instalação, marque **Add Python to PATH**.
3. Dê dois cliques em `INICIAR_MERCADO.bat`.
4. Uma janela de terminal ficará aberta com o servidor. O navegador deverá abrir `http://127.0.0.1:8765`.
5. No primeiro acesso, crie seu usuário administrador. A senha deve ter no mínimo 10 caracteres. Não existe senha padrão.

Para encerrar, pressione `Ctrl+C` na janela do servidor. Não apague a pasta `mercado/data`.

## Iniciar manualmente

Windows:

```powershell
cd mercado
py -3 app.py
```

Linux/macOS:

```sh
cd mercado
python3 app.py
```

Depois abra `http://127.0.0.1:8765` no navegador.

## Funcionalidades incluídas

- **Visão geral:** faturamento, lucro bruto estimado, despesas, resultado estimado, evolução diária, produtos em destaque, vendas recentes e alerta de estoque baixo.
- **Produtos:** cadastro, SKU único, código de barras opcional, categoria, unidade, custo, preço, estoque inicial, mínimo, busca, edição e arquivamento.
- **Categorias:** criar, editar, definir cor e impedir exclusão quando ainda estiver vinculada a produtos.
- **Promoções:** descontos percentuais por produto ou categoria, com datas opcionais de início/fim. As promoções não se acumulam; vale a maior oferta válida.
- **Estoque:** entradas, devoluções, contagem física/ajuste, histórico de antes/depois e motivo por movimento. Não permite saldo negativo.
- **Frente de caixa:** carrinho, desconto automático, formas de pagamento, cliente opcional, validação do preço e do saldo no servidor, venda e baixa de estoque atômicas.
- **Vendas:** histórico por período, detalhes por item e cancelamento com motivo e estorno das quantidades ao estoque.
- **Compras:** cadastro de fornecedores, rascunhos de pedidos e recebimento que atualiza quantidade e último custo dos produtos.
- **Clientes:** cadastro e vínculo opcional com vendas.
- **Financeiro gerencial:** registro de despesas, faturamento por categoria, formas de pagamento, custo, margem e resultado estimado.
- **Administração:** usuários com perfis de administrador, gerente e operador de caixa; registro de auditoria; exportação CSV e backup completo do banco SQLite.
- **Acessibilidade:** HTML semântico, formulário com rótulos, navegação por teclado, foco visível, atalho para pular ao conteúdo, anúncios de mensagens, suporte a redução de movimento e layout responsivo.

## Onde ficam os dados

Por padrão, o banco persistente fica em:

```text
mercado/data/mercado.sqlite3
```

O diretório guarda os registros reais da aplicação entre reinicializações. O SQLite usa transações, chaves estrangeiras, modo WAL e sincronização forte. Preços, custos e totais são calculados em centavos inteiros; quantidades são armazenadas em milésimos para evitar erros de ponto flutuante. Uma venda e sua baixa de estoque são gravadas na mesma transação. Se a operação falhar, as alterações não são confirmadas.

**Não edite o arquivo do banco manualmente enquanto o sistema estiver em execução.** Para backup, entre em **Administração → Backup completo do banco**. O download é uma cópia SQLite independente. Guarde-a em outro dispositivo ou pasta segura. Os CSVs podem ser abertos em planilhas; valores monetários dos CSVs são exportados em centavos para preservar precisão.

Por padrão, o programa aceita conexões apenas do próprio computador (`127.0.0.1`). Para operação em vários caixas/computadores, seria necessário implantar uma versão com servidor de rede, TLS, políticas de acesso e rotina de backup centralizada. Não exponha esta versão diretamente à internet.

## Segurança e perfis

- A senha é armazenada com hash PBKDF2-HMAC-SHA256 e salt individual, nunca como texto puro.
- A sessão usa cookie `HttpOnly` e `SameSite=Strict`, expira em 12 horas e tem proteção CSRF nas operações autenticadas de escrita.
- `Administrador`: acesso completo, usuários e auditoria.
- `Gerente`: operações de gestão, estoque, compras, vendas e despesas.
- `Operador de caixa`: pode finalizar vendas, mas não editar estoque ou catálogo, nem cancelar vendas.

## Limites importantes

O sistema é um **painel operacional/gerencial local**, não um ERP fiscal completo. Não inclui emissão homologada de NF-e/NFC-e/SAT, cálculo tributário, TEF/adquirentes, integração certificada com balanças, controle de lotes/validade, folha de pagamento ou conciliação bancária. Essas funções exigem regras, equipamentos e integrações específicas. O resultado financeiro é uma estimativa baseada nos custos registrados e nas despesas que forem lançadas.

O leitor de código de barras pode ser usado como entrada de teclado em campos de busca/código, mas não há integração dedicada com hardware fiscal. Faça a conferência do estoque e teste seus procedimentos antes de usar o sistema em operação comercial real.

## Testes automatizados

O projeto inclui um teste de integração sem dependências externas:

```sh
python tests/smoke_test.py
```

Ele testa cadastro, descontos, venda, baixa, cancelamento/estorno, recebimento de compra, despesas, permissões, backup, CSV e persistência após reiniciar o processo.
