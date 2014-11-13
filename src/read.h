#ifndef READ_H
#define READ_H

#include <string>
#include <vector>
#include <unordered_set>

#include "entry.h"

class Read {
public:
	Read(const std::string& name, int mapq);
	virtual ~Read() {}
	std::string toString();
	void addVariant(int position, char base, int allele, int quality);
	void sortVariants();
	/** Returns the position of the first variant. **/
	int firstPosition() const;
	/** Returns the position of the last variant. **/
	int lastPosition() const;
	void setID(int id);
	int getID() const;
	/** Add all positions contained in this read to the given set. */
	void addPositionsToSet(std::unordered_set<unsigned int>* set);
	int getPosition(size_t variant_idx) const;
	const Entry* getEntry(size_t variant_idx) const;
	int getVariantCount() const;
private:
	typedef struct enriched_entry_t {
		Entry entry;
		int position;
		char base;
		enriched_entry_t(int position, char base, int allele, int quality) : entry(0,Entry::allele_t(allele),quality), position(position), base(base) {}
	} enriched_entry_t;
	
	typedef struct entry_comparator_t {
		entry_comparator_t() {}
		bool operator()(const enriched_entry_t& e1, const enriched_entry_t& e2) {
			return e1.position < e2.position;
		}
	} entry_comparator_t;

	std::string name;
	int mapq;
	int id;
	std::vector<enriched_entry_t> variants;
};

#endif
